# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2018 The OpenAI Team Authors and HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import numpy as np
import paddle
from paddle import nn
from paddlenlp_ops import fused_get_rotary_embedding, get_padding_offset

from paddlenlp.experimental.transformers.fused_transformer_layers import (
    FusedMultiTransformerBase,
    FusedMultiTransformerConfig,
)
from paddlenlp.experimental.transformers.generation_utils import (
    GenerationInferenceModel,
)
from paddlenlp.transformers.model_outputs import (
    BaseModelOutputWithPast,
    CausalLMOutputWithPast,
)
from paddlenlp.transformers import QWenPretrainedModel
from paddlenlp.transformers.model_utils import (
    dy2st_nocheck_guard_context,
    register_base_model,
)
from paddlenlp.transformers.qwen.configuration import QWenConfig
from paddlenlp.transformers.qwen.modeling import QWenLMHead, QWenPretrainingCriterion

__all__ = ["QWenForCausalLMInferenceModel"]

class FusedQWenRMSNorm(nn.Layer):
    def __init__(self, config):
        super().__init__()
        self.eps = config.layer_norm_epsilon
        self.weight = paddle.create_parameter(
            shape=[config.hidden_size],
            dtype=paddle.get_default_dtype(),
            default_initializer=nn.initializer.Constant(1.0),
        )
    
    def forward(self, x):
        result = paddle.incubate.nn.functional.fused_rms_norm(
            x, self.weight, None, self.eps, begin_norm_axis=1
        )
        if isinstance(result, tuple):
            return result[0]
        return result

"""
dict_keys(['lm_head.weight', 
'qwen.h.0.attn.c_attn.bias', 'qwen.h.0.attn.c_attn.weight', 'qwen.h.0.attn.c_proj.weight', 'qwen.h.0.ln_1.weight', 'qwen.h.0.ln_2.weight', 'qwen.h.0.mlp.c_proj.weight', 'qwen.h.0.mlp.w1.weight', 'qwen.h.0.mlp.w2.weight', 
'qwen.h.1.attn.c_attn.bias', 'qwen.h.1.attn.c_attn.weight', 'qwen.h.1.attn.c_proj.weight', 'qwen.h.1.ln_1.weight', 'qwen.h.1.ln_2.weight', 'qwen.h.1.mlp.c_proj.weight', 'qwen.h.1.mlp.w1.weight', 'qwen.h.1.mlp.w2.weight', 
'qwen.h.2.attn.c_attn.bias', 'qwen.h.2.attn.c_attn.weight', 'qwen.h.2.attn.c_proj.weight', 'qwen.h.2.ln_1.weight', 'qwen.h.2.ln_2.weight', 'qwen.h.2.mlp.c_proj.weight', 'qwen.h.2.mlp.w1.weight', 'qwen.h.2.mlp.w2.weight', 
'qwen.h.3.attn.c_attn.bias', 'qwen.h.3.attn.c_attn.weight', 'qwen.h.3.attn.c_proj.weight', 'qwen.h.3.ln_1.weight', 'qwen.h.3.ln_2.weight', 'qwen.h.3.mlp.c_proj.weight', 'qwen.h.3.mlp.w1.weight', 'qwen.h.3.mlp.w2.weight', 
'qwen.ln_f.weight', 'qwen.wte.weight'])
"""
@register_base_model
class QWenInferenceModel(QWenPretrainedModel):
    def __init__(self, config: QWenConfig):
        super(QWenPretrainedModel, self).__init__(config)
        self.vocab_size = config.vocab_size
        self.hidden_size = config.hidden_size
        self.num_attention_heads = config.num_attention_heads
        self.intermediate_size = config.intermediate_size
        self.num_layers = config.num_hidden_layers
        self.layer_norm_epsilon = config.layer_norm_epsilon
        self.max_position_embeddings = config.max_position_embeddings
        self.emb_dropout_prob = config.emb_dropout_prob

        self.wte = nn.Embedding(self.vocab_size, self.hidden_size)
        self.drop = nn.Dropout(self.emb_dropout_prob)

        ln_scale_attrs = [paddle.ParamAttr(name="fuseqwen.{}.ln_scale".format(i)) for i in range(self.num_layers)]
        qkv_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen.{}.qkv_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]
        qkv_bias_attrs = [
            paddle.ParamAttr(name="fuseqwen.{}.qkv_bias".format(i))
            for i in range(self.num_layers)
        ]
        out_proj_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen.{}.out_proj_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]
        ffn_ln_scale_attrs = [
            paddle.ParamAttr(name="fuseqwen.{}.ffn_ln_scale".format(i)) for i in range(self.num_layers)
        ]
        ffn1_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen.{}.ffn1_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]
        ffn2_weight_attrs = [
            paddle.ParamAttr(
                name="fuseqwen.{}.ffn2_weight".format(i), initializer=paddle.nn.initializer.Constant(value=0)
            )
            for i in range(self.num_layers)
        ]

        transformer_config = FusedMultiTransformerConfig(
            self.hidden_size,
            self.num_attention_heads,
            self.intermediate_size,
            activation="swiglu",
            num_layers=config.num_hidden_layers,
            nranks=1,
            ring_id=-1,
            ln_scale_attrs=ln_scale_attrs,
            qkv_weight_attrs=qkv_weight_attrs,
            linear_weight_attrs=out_proj_weight_attrs,
            ffn_ln_scale_attrs=ffn_ln_scale_attrs,
            ffn1_weight_attrs=ffn1_weight_attrs,
            ffn2_weight_attrs=ffn2_weight_attrs,
            qkv_bias_attrs=qkv_bias_attrs,
            epsilon=self.layer_norm_epsilon,
            norm_type="rmsnorm",
            use_neox_rotary_style=True,
        )

        self.transformer_block = FusedMultiTransformerBase(transformer_config)

        self.ln_f = FusedQWenRMSNorm(config)
        
        self.split_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        
        self.cache_kvs = None
        self.head_dim_shape_tensor = paddle.ones((self.hidden_size // self.num_attention_heads), dtype="int8")

    def get_input_embeddings(self):
        return self.wte

    def set_input_embeddings(self, value):
        self.wte = value

    @paddle.no_grad()
    def set_state_dict(self, state_dict):
        head_size = self.hidden_size // self.num_attention_heads

        self.wte.weight.set_value(paddle.to_tensor(state_dict["qwen.wte.weight"]))
        self.ln_f.weight.set_value(paddle.to_tensor(state_dict["qwen.ln_f.weight"]), dtype=self.ln_f.weight.dtype)

        for idx in range(self.num_layers):
            ln_scale = paddle.to_tensor(
                state_dict["qwen.h.{}.ln_1.weight".format(idx)], dtype=self.transformer_block.ln_scales[idx].dtype
            )
            qkv_weight = paddle.to_tensor(state_dict["qwen.h.{}.attn.c_attn.weight".format(idx)])
            qkv_bias = paddle.to_tensor(state_dict["qwen.h.{}.attn.c_attn.bias".format(idx)])

            linear_weight = paddle.to_tensor(state_dict["qwen.h.{}.attn.c_proj.weight".format(idx)])

            ffn_ln_scale = paddle.to_tensor(
                state_dict["qwen.h.{}.ln_2.weight".format(idx)], dtype=self.transformer_block.ffn_ln_scales[idx].dtype
            )

            up_weight = state_dict["qwen.h.{}.mlp.w1.weight".format(idx)]
            gate_weight = state_dict["qwen.h.{}.mlp.w2.weight".format(idx)]
            concated_ffn1_weight = np.concatenate([up_weight, gate_weight], axis=-1)
            ffn1_weight = paddle.to_tensor(concated_ffn1_weight)
            ffn2_weight = paddle.to_tensor(state_dict["qwen.h.{}.mlp.c_proj.weight".format(idx)])

            self.transformer_block.ln_scales[idx].set_value(ln_scale)
            
            self.transformer_block.qkv_weights[idx].set_value(qkv_weight)
            self.transformer_block.qkv_biases[idx].set_value(qkv_bias)

            self.transformer_block.linear_weights[idx].set_value(linear_weight)

            self.transformer_block.ffn_ln_scales[idx].set_value(ffn_ln_scale)

            self.transformer_block.ffn1_weights[idx].set_value(ffn1_weight)
            self.transformer_block.ffn2_weights[idx].set_value(ffn2_weight)
    
    def remove_padding(self, input_ids, seq_lens_this_time):
        cum_offsets_now = paddle.cumsum(paddle.max(seq_lens_this_time) - seq_lens_this_time)
        token_num = paddle.sum(seq_lens_this_time)
        ids_remove_padding, cum_offsets, padding_offset = get_padding_offset(
            input_ids, cum_offsets_now, token_num, seq_lens_this_time
        )
        return ids_remove_padding, padding_offset, cum_offsets
    
    def get_masks(self, batch_size, seq_length, past_length, padding_mask=None):
        # casual mask
        casual_mask = paddle.tril(paddle.ones([batch_size, 1, seq_length, seq_length], dtype="bool"))
        if past_length > 0:
            casual_mask = paddle.concat(
                [paddle.ones([batch_size, 1, seq_length, past_length], dtype="bool"), casual_mask], axis=-1
            )

        # seq_mask
        if padding_mask is None:
            padding_mask = paddle.ones((batch_size, 1, seq_length, seq_length + past_length), dtype="bool")
        if len(padding_mask.shape) == 2:
            # from Tokenizer
            padding_mask = (
                padding_mask.unsqueeze(axis=[1, 2])
                .expand([batch_size, 1, seq_length, seq_length + past_length])
                .astype("bool")
            )
        elif len(padding_mask.shape) == 3:
            # [batch_size,tgt_length, src_length] -> [batch_size, 1, tgt_length, src_length]
            padding_mask = padding_mask.unsqueeze(1).astype("bool")
        elif len(padding_mask.shape) == 4:
            padding_mask = padding_mask.astype("bool")

        casual_mask = casual_mask & padding_mask

        return casual_mask
    
    def forward(
        self,
        input_ids=None,
        position_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        use_cache=None,
        cache_kvs=None,
        pre_caches=None,
        seq_len_encoder=None,
        seq_len_decoder=None,
        past_key_values=None,
        output_attentions=False,
        output_hidden_states=None,
        return_dict=False,
        **kwargs,
    ):
        # kwargs["cache"] is used used to distinguish between encoder and decoder phase.
        past_key_values = kwargs.get("cache", None)
        is_decoder = past_key_values is not None
        
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is None and inputs_embeds is None:
            raise ValueError("You have to specify either input_ids or inputs_embeds")
        
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None:
            input_shape = input_ids.shape
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
            batch, seq_len, hidden_dim = inputs_embeds.shape
            inputs_embeds = inputs_embeds.reshape([batch * seq_len, hidden_dim])
        
        if past_key_values is None:
            past_length = 0
            past_key_values = tuple([None] * len(self.config.num_hidden_layers))
        else:
            past_length = past_key_values[0][0].shape[1]
        
        if not is_decoder:
            ids_remove_padding, padding_offset, cum_offsets = self.remove_padding(input_ids, seq_len_encoder)
        else:
            ids_remove_padding = input_ids
            padding_offset = None
            cum_offsets = None
        
        if inputs_embeds is None:
            inputs_embeds = self.wte(ids_remove_padding)
        hidden_states = inputs_embeds

        # bool 4D mask
        attention_mask = self.get_masks(input_shape[0], input_shape[1], past_length, padding_mask=attention_mask)
        zero = paddle.zeros(attention_mask.shape, dtype=hidden_states.dtype)
        neg_inf = paddle.full_like(attention_mask, paddle.finfo(hidden_states.dtype).min, dtype=hidden_states.dtype)
        # dtype 4D mask
        attention_mask = paddle.where(attention_mask, zero, neg_inf)

        output_shape = input_shape + [hidden_states.shape[-1]]

        # decoder layers
        presents = () if use_cache else None
        all_hidden_states = () if output_hidden_states else None
        all_self_attentions = () if output_attentions else None

        seq_lens = seq_len_decoder if is_decoder else seq_len_encoder
        
        position_offset = 0
        if not is_decoder and pre_caches is not None:
            position_offset = 128

        new_rope = fused_get_rotary_embedding(
            input_ids, position_ids, self.head_dim_shape_tensor, position_offset, True
        )

        with dy2st_nocheck_guard_context():
            hidden_states, _ = self.transformer_block(
                input_ids,
                hidden_states,
                cum_offsets=cum_offsets,
                padding_offset=padding_offset,
                attn_mask=paddle.cast(attention_mask, dtype=hidden_states.dtype),
                caches=cache_kvs,
                pre_caches=pre_caches,
                pre_caches_length=position_offset,
                seq_lens=seq_lens,
                rotary_embs=new_rope,
                rotary_emb_dims=1,
                time_step=paddle.increment(paddle.shape(attention_mask)[-1], -1) if is_decoder else None,
            )
        
        hidden_states = self.ln_f(hidden_states)
        hidden_states = hidden_states.reshape(output_shape)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)
        
        if not return_dict:
            return tuple(v for v in [hidden_states, presents, all_hidden_states, all_self_attentions] if v is not None)
        
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
        )

class QWenForCausalLMInferenceModel(GenerationInferenceModel, QWenPretrainedModel):
    def __init__(self, config: QWenConfig, **kwargs):
        super(QWenForCausalLMInferenceModel, self).__init__(config)
        self.qwen = QWenInferenceModel(config)
        self.lm_head = QWenLMHead(config)
        self.criterion = QWenPretrainingCriterion(config)
    
    def get_output_embeddings(self):
        return self.lm_head
    
    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    @classmethod
    def from_pretrained(
        cls, pretrained_model_name_or_path, from_hf_hub: bool = False, subfolder: str | None = None, *args, **kwargs
    ):
        # TODO: Support safetensors loading.
        kwargs["use_safetensors"] = False
        return super().from_pretrained(pretrained_model_name_or_path, from_hf_hub, subfolder, *args, **kwargs)
    
    @classmethod
    def get_cache_kvs_shape(
        cls, config: QWenConfig, max_batch_size: int = None, max_length: int = None
    ) -> list[list[int]]:
        """get cache_kvs tensor for qwen model

        Args:
            max_batch_size (int): the max batch size
            max_length (int | None, optional): the max_length of cache_kvs. Defaults to None.

        Returns:
            list[paddle.Tensor]: the list tensor shape for cache
        """
        if max_length is None:
            max_length = config.max_position_embeddings
        
        cache_kvs = []
        for _ in range(config.num_hidden_layers):
            cache_kvs.append(
                [
                    2,
                    max_batch_size,
                    config.num_attention_heads // max(config.tensor_parallel_degree, 1),
                    max_length,
                    config.hidden_size // config.num_attention_heads,
                ]
            )
        return cache_kvs

    def prepare_inputs_for_generation(
        self,
        input_ids,
        cache_kvs,
        seq_len_encoder,
        seq_len_decoder,
        tgt_ids,
        tgt_pos,
        tgt_generation_mask,
        **kwargs,
    ):
        position_ids = kwargs.get("position_ids", None)
        attention_mask = kwargs.get("attention_mask", None)
        cache = kwargs.get("cache", None)
        pre_caches = kwargs.get("pre_caches", None)
        inputs_embeds = kwargs.get("inputs_embeds", None)
        if cache is not None:
            input_ids = tgt_ids
            position_ids = tgt_pos
            attention_mask = (tgt_generation_mask - 1) * 1e4
            # make inputs_embeds be none in decoder phase.
            # in forward function, it will be assigned according to input_ids.
            inputs_embeds = None
        else:
            attention_mask = (attention_mask - 1) * 1e4
        model_inputs = {
            "input_ids": input_ids,
            "inputs_embeds": inputs_embeds,
            "position_ids": position_ids,
            "attention_mask": attention_mask,
            "cache_kvs": cache_kvs,
            "seq_len_encoder": seq_len_encoder,
            "seq_len_decoder": seq_len_decoder,
            "cache": cache,
            "pre_caches": pre_caches,
        }
        return model_inputs

    def forward(
        self,
        input_ids,
        position_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        use_cache=False,
        cache=None,
        cache_kvs=None,
        pre_caches=None,
        seq_len_encoder=None,
        seq_len_decoder=None,
        past_key_values=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.qwen(
            input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            cache=cache,
            cache_kvs=cache_kvs,
            pre_caches=pre_caches,
            seq_len_encoder=seq_len_encoder,
            seq_len_decoder=seq_len_decoder,
            past_key_values=past_key_values,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]

        # if labels is None，means we need full output, instead of tensor_parallel_output
        # tensor_parallel_output is togather with ParallelCrossEntropy
        tensor_parallel_output = (
            self.config.tensor_parallel_output and labels is not None and self.config.tensor_parallel_degree > 1
        )
        lm_logits = self.lm_head(hidden_states, tensor_parallel_output=tensor_parallel_output)

        loss = None
        if labels is not None:
            loss = self.criterion(lm_logits, labels)

        if not return_dict:
            output = (lm_logits,) + outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=lm_logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    @paddle.no_grad()
    def set_state_dict(self, state_dict):
        if "lm_head.weight" in state_dict:
            self.lm_head.weight.set_value(state_dict["lm_head.weight"])
        self.qwen.set_state_dict({k: state_dict[k] for k in state_dict.keys()})
        