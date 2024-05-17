import torch
from transformers import DynamicCache

from ....utils import is_flash_attention_available
from ...enums import AttentionHeadType, PositionEmbeddingType
from ..position_embedding import apply_rotary_pos_emb
from .base import Attention
from .flash_utils import flash_attention, get_unpad_data


if is_flash_attention_available():
    from flash_attn.bert_padding import index_first_axis, pad_input, unpad_input


class FlashAttention2(Attention):
    def forward(
        self,
        hidden_states: torch.Tensor,
        past_key_values: DynamicCache = None,
        attention_mask: torch.Tensor = None,
        rope_cos_sin: torch.Tensor = None,
        cu_seqlens: torch.Tensor = None,
        max_seqlen: torch.Tensor = None,
    ) -> torch.Tensor:
        # ==========================================================================================
        # hidden_states -> (batch_size, query_length, num_heads * head_dim)
        # ==========================================================================================

        query, key, value = self._prepare_qkv_for_forward(hidden_states)

        # ==========================================================================================
        # query -> (batch_size, num_heads, query_length, head_dim)
        # key -> (batch_size, num_key_value_heads, query_length, head_dim)
        # value -> (batch_size, num_key_value_heads, query_length, head_dim)
        # ==========================================================================================

        if self.position_embedding_type == PositionEmbeddingType.rope:
            query = apply_rotary_pos_emb(query, rope_cos_sin)
            key = apply_rotary_pos_emb(key, rope_cos_sin)

        if past_key_values is not None:
            key, value = past_key_values.update(key, value, self.layer_idx)

        # ==========================================================================================
        # query -> (batch_size, num_heads, query_length, head_dim)
        # key -> (batch_size, num_key_value_heads, key_length, head_dim)
        # value -> (batch_size, num_key_value_heads, key_length, head_dim)
        # ==========================================================================================

        # TODO avoid this extra transpose
        query = query.transpose(1, 2)
        if self.attention_head_type == AttentionHeadType.mqa:
            key = key.squeeze(1).unsqueeze(2)
            value = value.squeeze(1).unsqueeze(2)
        else:
            key = key.transpose(1, 2)
            value = value.transpose(1, 2)

        # ==========================================================================================
        # query -> (batch_size, query_length, num_heads, head_dim)
        # key -> (batch_size, key_length, num_heads, head_dim)
        # value -> (batch_size, key_length, num_heads, head_dim)
        # ==========================================================================================

        softmax_scale = self._get_softmax_scale()
        dropout_p = self.attn_pdrop if self.training else 0

        batch_size, query_length = query.shape[:2]

        key_length = key.shape[1]

        indices_k, cu_seqlens_k, max_seqlen_k = get_unpad_data(attention_mask)

        key = index_first_axis(
            key.reshape(batch_size * key_length, self.num_key_value_heads, self.head_dim), indices_k
        )
        value = index_first_axis(
            value.reshape(batch_size * key_length, self.num_key_value_heads, self.head_dim), indices_k
        )

        if query_length == key_length:
            query = index_first_axis(query.reshape(batch_size * key_length, self.num_heads, self.head_dim), indices_k)
            cu_seqlens_q = cu_seqlens_k
            max_seqlen_q = max_seqlen_k
            indices_q = indices_k
        elif query_length == 1:
            max_seqlen_q = 1
            cu_seqlens_q = torch.arange(
                batch_size + 1, dtype=torch.int32, device=query.device
            )  # There is a memcpy here, that is very bad.
            indices_q = cu_seqlens_q[:-1]
            query = query.squeeze(1)
        else:
            # The -q_len: slice assumes left padding.
            attention_mask = attention_mask[:, -query_length:]
            query, indices_q, cu_seqlens_q, max_seqlen_q = unpad_input(query, attention_mask)

        # ==========================================================================================
        # query -> (total_q, num_heads, head_dim)
        # key -> (total_q, num_heads, head_dim)
        # value -> (total_q, num_heads, head_dim)
        # ==========================================================================================

        attn_output = flash_attention(
            query,
            key,
            value,
            cu_seqlens_q=cu_seqlens_q,
            cu_seqlens_k=cu_seqlens_k,
            max_seqlen_q=max_seqlen_q,
            max_seqlen_k=max_seqlen_k,
            dropout_p=dropout_p,
            softmax_scale=softmax_scale,
            causal=self.causal,
        )

        # ==========================================================================================
        # attn_output -> (total_q, num_heads, head_dim)
        # ==========================================================================================

        attn_output = pad_input(attn_output, indices_q, batch_size, query_length)

        attn_output = attn_output.view(batch_size, query_length, -1)

        # ==========================================================================================
        # attn_output -> (batch_size, query_length, num_heads * head_dim)
        # ==========================================================================================

        attn_output = self.c_proj(attn_output)
        attn_output = self.resid_dropout(attn_output)

        return attn_output
