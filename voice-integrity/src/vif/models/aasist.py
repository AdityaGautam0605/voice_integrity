"""AASIST detection head.

Re-implementation of the graph-attention spoofing countermeasure described in
Jung et al., "AASIST: Audio Anti-Spoofing using Integrated Spectro-Temporal
Graph Attention Networks" (ICASSP 2022), in the self-supervised front-end
configuration of Tak et al. (Odyssey 2022).

Why this architecture rather than a pooling MLP: synthesis artifacts are
jointly spectral and temporal.  A vocoder leaves correlated traces across
frequency bands *and* across time, and the heterogeneous graph attention here
models both, plus their interaction, instead of collapsing one away.

About 430K trainable parameters, most of them in the 1024->128 input projection.  Cheap to train, cheap to run, and small
enough that the on-device tier can carry it without the front end.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


def _new_parameter(*size: int) -> nn.Parameter:
    out = nn.Parameter(torch.empty(*size))
    nn.init.xavier_normal_(out)
    return out


class GraphAttentionLayer(nn.Module):
    """Attention over a fully connected graph of nodes.

    Nodes are frequency bins (spectral branch) or time steps (temporal
    branch).  The attention map is derived from pairwise products, so the
    layer learns which band-pairs or frame-pairs carry the artifact.
    """

    def __init__(self, in_dim: int, out_dim: int, temperature: float = 1.0, dropout: float = 0.2):
        super().__init__()
        self.temperature = temperature
        self.att_proj = nn.Linear(in_dim, out_dim)
        self.att_weight = _new_parameter(out_dim, 1)
        self.proj_with_att = nn.Linear(in_dim, out_dim)
        self.proj_without_att = nn.Linear(in_dim, out_dim)
        self.bn = nn.BatchNorm1d(out_dim)
        self.input_drop = nn.Dropout(p=dropout)
        self.act = nn.SELU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, N, in_dim)
        x = self.input_drop(x)
        att_map = self._attention(x)
        x = self._project(x, att_map)
        x = self._batch_norm(x)
        return self.act(x)

    def _attention(self, x: torch.Tensor) -> torch.Tensor:
        n = x.size(1)
        a = x.unsqueeze(1).expand(-1, n, -1, -1)
        b = a.transpose(1, 2)
        pairwise = a * b  # (B, N, N, in_dim)
        att = torch.tanh(self.att_proj(pairwise))
        att = torch.matmul(att, self.att_weight)  # (B, N, N, 1)
        return F.softmax(att / self.temperature, dim=-2)

    def _project(self, x: torch.Tensor, att_map: torch.Tensor) -> torch.Tensor:
        weighted = torch.matmul(att_map.squeeze(-1), x)
        return self.proj_with_att(weighted) + self.proj_without_att(x)

    def _batch_norm(self, x: torch.Tensor) -> torch.Tensor:
        shape = x.shape
        x = x.view(-1, shape[-1])
        x = self.bn(x)
        return x.view(shape)


class HtrgGraphAttentionLayer(nn.Module):
    """Heterogeneous attention across two node types plus a master node.

    This is what makes AASIST more than two parallel GATs: the spectral and
    temporal graphs exchange information directly, and a master node
    aggregates the whole graph so a global artifact signature can be read out.
    """

    def __init__(self, in_dim: int, out_dim: int, temperature: float = 1.0, dropout: float = 0.2):
        super().__init__()
        self.temperature = temperature

        self.proj_type1 = nn.Linear(in_dim, in_dim)
        self.proj_type2 = nn.Linear(in_dim, in_dim)

        self.att_proj = nn.Linear(in_dim, out_dim)
        self.att_projM = nn.Linear(in_dim, out_dim)

        self.att_weight11 = _new_parameter(out_dim, 1)
        self.att_weight22 = _new_parameter(out_dim, 1)
        self.att_weight12 = _new_parameter(out_dim, 1)
        self.att_weightM = _new_parameter(out_dim, 1)

        self.proj_with_att = nn.Linear(in_dim, out_dim)
        self.proj_without_att = nn.Linear(in_dim, out_dim)
        self.proj_with_attM = nn.Linear(in_dim, out_dim)
        self.proj_without_attM = nn.Linear(in_dim, out_dim)

        self.bn = nn.BatchNorm1d(out_dim)
        self.input_drop = nn.Dropout(p=dropout)
        self.act = nn.SELU(inplace=True)

    def forward(
        self,
        x1: torch.Tensor,
        x2: torch.Tensor,
        master: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        n1, n2 = x1.size(1), x2.size(1)
        # Type-specific projections first, so spectral and temporal nodes enter
        # the shared attention from their own learned spaces.
        x = torch.cat([self.proj_type1(x1), self.proj_type2(x2)], dim=1)
        if master is None:
            master = torch.mean(x, dim=1, keepdim=True)

        x = self.input_drop(x)
        att_map = self._attention(x, n1, n2)
        master_map = self._master_attention(x, master)

        master = self._update_master(x, master, master_map)
        x = self._project(x, att_map)
        x = self.act(self._batch_norm(x))

        return x.narrow(1, 0, n1), x.narrow(1, n1, n2), master

    def _attention(self, x: torch.Tensor, n1: int, n2: int) -> torch.Tensor:
        n = x.size(1)
        a = x.unsqueeze(1).expand(-1, n, -1, -1)
        b = a.transpose(1, 2)
        pairwise = a * b
        att = torch.tanh(self.att_proj(pairwise))

        # Node-type-specific weights: within type 1, within type 2, and across.
        att11 = torch.matmul(att[:, :n1, :n1, :], self.att_weight11)
        att22 = torch.matmul(att[:, n1:, n1:, :], self.att_weight22)
        att12 = torch.matmul(att[:, :n1, n1:, :], self.att_weight12)
        att21 = torch.matmul(att[:, n1:, :n1, :], self.att_weight12)

        top = torch.cat([att11, att12], dim=2)
        bottom = torch.cat([att21, att22], dim=2)
        att = torch.cat([top, bottom], dim=1)
        return F.softmax(att / self.temperature, dim=-2)

    def _master_attention(self, x: torch.Tensor, master: torch.Tensor) -> torch.Tensor:
        att = x * master
        att = torch.tanh(self.att_projM(att))
        att = torch.matmul(att, self.att_weightM)
        return F.softmax(att / self.temperature, dim=-2)

    def _update_master(
        self, x: torch.Tensor, master: torch.Tensor, master_map: torch.Tensor
    ) -> torch.Tensor:
        weighted = torch.matmul(master_map.transpose(-2, -1), x)
        # The residual path carries the previous master state, as in the paper.
        # Feeding it the attended nodes again left the master with no memory.
        return self.proj_with_attM(weighted) + self.proj_without_attM(master)

    def _project(self, x: torch.Tensor, att_map: torch.Tensor) -> torch.Tensor:
        weighted = torch.matmul(att_map.squeeze(-1), x)
        return self.proj_with_att(weighted) + self.proj_without_att(x)

    def _batch_norm(self, x: torch.Tensor) -> torch.Tensor:
        shape = x.shape
        x = x.view(-1, shape[-1])
        x = self.bn(x)
        return x.view(shape)


class GraphPool(nn.Module):
    """Top-k node selection, scored by a learned projection."""

    def __init__(self, ratio: float, in_dim: int, dropout: float = 0.2):
        super().__init__()
        self.ratio = ratio
        self.proj = nn.Linear(in_dim, 1)
        self.drop = nn.Dropout(p=dropout)
        self.sigmoid = nn.Sigmoid()

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        scores = self.sigmoid(self.proj(self.drop(h)))
        k = min(h.size(1), max(2, int(h.size(1) * self.ratio)))
        _, idx = torch.topk(scores.squeeze(-1), k, dim=1)
        idx_expanded = idx.unsqueeze(-1).expand(-1, -1, h.size(-1))
        selected = torch.gather(h, 1, idx_expanded)
        gate = torch.gather(scores, 1, idx.unsqueeze(-1))
        return selected * gate


class ResidualBlock(nn.Module):
    """2-D residual block over the (frequency, time) feature map."""

    def __init__(self, in_ch: int, out_ch: int, first: bool = False):
        super().__init__()
        self.first = first
        if not first:
            self.bn1 = nn.BatchNorm2d(in_ch)
        self.conv1 = nn.Conv2d(in_ch, out_ch, kernel_size=(2, 3), padding=(1, 1))
        self.bn2 = nn.BatchNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, kernel_size=(2, 3), padding=(0, 1))
        self.act = nn.SELU(inplace=True)
        self.downsample = in_ch != out_ch
        if self.downsample:
            self.conv_down = nn.Conv2d(in_ch, out_ch, kernel_size=(1, 3), padding=(0, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        identity = x
        out = x if self.first else self.act(self.bn1(x))
        out = self.conv1(out)
        out = self.act(self.bn2(out))
        out = self.conv2(out)
        if self.downsample:
            identity = self.conv_down(identity)
        out = out + identity
        return out


class AASIST(nn.Module):
    """Spoof detection head over self-supervised features.

    Input  : (B, T, feat_dim) hidden states from the SSL front end
    Output : (B, 2) logits, and the pooled embedding for inspection

    The scalar score used everywhere downstream is
    ``logits[:, 0] - logits[:, 1]`` (class 0 is spoof), oriented so that
    **higher means more synthetic**.  Keep that orientation consistent or every
    calibration constant silently flips sign.
    """

    def __init__(
        self,
        feat_dim: int = 1024,
        proj_dim: int = 128,
        filts: tuple[int, ...] = (70, 32, 32, 64, 64),
        gat_dims: tuple[int, int] = (64, 32),
        pool_ratios: tuple[float, float, float, float] = (0.5, 0.7, 0.5, 0.5),
        temperatures: tuple[float, float, float, float] = (2.0, 2.0, 100.0, 100.0),
        dropout: float = 0.2,
    ):
        super().__init__()
        self.feat_dim = feat_dim

        # Project SSL features down, then treat (proj_dim, T) as an image.
        self.proj = nn.Linear(feat_dim, proj_dim)
        self.first_bn = nn.BatchNorm2d(1)
        self.act = nn.SELU(inplace=True)

        self.encoder = nn.Sequential(
            ResidualBlock(1, filts[1], first=True),
            ResidualBlock(filts[1], filts[2]),
            ResidualBlock(filts[2], filts[3]),
            ResidualBlock(filts[3], filts[4]),
            ResidualBlock(filts[4], filts[4]),
            ResidualBlock(filts[4], filts[4]),
        )
        enc_dim = filts[4]

        # Positional embeddings for the two node types.
        self.pos_S = _new_parameter(1, proj_dim // 3, enc_dim)

        self.GAT_S = GraphAttentionLayer(enc_dim, gat_dims[0], temperature=temperatures[0])
        self.GAT_T = GraphAttentionLayer(enc_dim, gat_dims[0], temperature=temperatures[1])

        self.HtrgGAT_ST_1 = HtrgGraphAttentionLayer(
            gat_dims[0], gat_dims[1], temperature=temperatures[2]
        )
        self.HtrgGAT_ST_2 = HtrgGraphAttentionLayer(
            gat_dims[1], gat_dims[1], temperature=temperatures[2]
        )
        self.HtrgGAT_ST_1b = HtrgGraphAttentionLayer(
            gat_dims[0], gat_dims[1], temperature=temperatures[3]
        )
        self.HtrgGAT_ST_2b = HtrgGraphAttentionLayer(
            gat_dims[1], gat_dims[1], temperature=temperatures[3]
        )

        self.pool_S = GraphPool(pool_ratios[0], gat_dims[0], dropout)
        self.pool_T = GraphPool(pool_ratios[1], gat_dims[0], dropout)
        self.pool_hS1 = GraphPool(pool_ratios[2], gat_dims[1], dropout)
        self.pool_hT1 = GraphPool(pool_ratios[2], gat_dims[1], dropout)
        self.pool_hS2 = GraphPool(pool_ratios[2], gat_dims[1], dropout)
        self.pool_hT2 = GraphPool(pool_ratios[2], gat_dims[1], dropout)

        self.master1 = _new_parameter(1, 1, gat_dims[0])
        self.master2 = _new_parameter(1, 1, gat_dims[0])

        self.drop = nn.Dropout(0.5)
        self.out = nn.Linear(5 * gat_dims[1], 2)

    def forward(self, x: torch.Tensor, return_embedding: bool = False):
        # x: (B, T, feat_dim)
        x = self.proj(x)  # (B, T, proj_dim)
        x = x.transpose(1, 2).unsqueeze(1)  # (B, 1, proj_dim, T)
        # One up-front pool, as in the SSL variant of AASIST.  The raw-waveform
        # original pools time by 3 inside every residual block, which is fine
        # for ~21k time steps but reduces 201 SSL frames to zero by block five.
        x = F.max_pool2d(x, (3, 3))  # (B, 1, proj_dim // 3, T // 3)
        x = self.act(self.first_bn(x))
        x = self.encoder(x)  # (B, C, F', T')

        # Spectral nodes: collapse time.  Temporal nodes: collapse frequency.
        e_S, _ = torch.max(torch.abs(x), dim=3)
        e_S = e_S.transpose(1, 2) + self.pos_S  # (B, F', C)
        e_T, _ = torch.max(torch.abs(x), dim=2)
        e_T = e_T.transpose(1, 2)  # (B, T', C)

        gat_S = self.pool_S(self.GAT_S(e_S))
        gat_T = self.pool_T(self.GAT_T(e_T))

        batch = x.size(0)
        master1 = self.master1.expand(batch, -1, -1)
        master2 = self.master2.expand(batch, -1, -1)

        # Two heterogeneous stacks with different temperatures, then a max
        # graph operation over them - the "integrated" part of the name.
        out_S1, out_T1, m1 = self.HtrgGAT_ST_1(gat_S, gat_T, master=master1)
        out_S1 = self.pool_hS1(out_S1)
        out_T1 = self.pool_hT1(out_T1)
        res_S1, res_T1, m1 = self.HtrgGAT_ST_2(out_S1, out_T1, master=m1)
        out_S1 = out_S1 + res_S1
        out_T1 = out_T1 + res_T1

        out_S2, out_T2, m2 = self.HtrgGAT_ST_1b(gat_S, gat_T, master=master2)
        out_S2 = self.pool_hS2(out_S2)
        out_T2 = self.pool_hT2(out_T2)
        res_S2, res_T2, m2 = self.HtrgGAT_ST_2b(out_S2, out_T2, master=m2)
        out_S2 = out_S2 + res_S2
        out_T2 = out_T2 + res_T2

        out_S = torch.max(out_S1, out_S2)
        out_T = torch.max(out_T1, out_T2)
        master = torch.max(m1, m2)

        s_max, _ = torch.max(torch.abs(out_S), dim=1)
        s_avg = torch.mean(out_S, dim=1)
        t_max, _ = torch.max(torch.abs(out_T), dim=1)
        t_avg = torch.mean(out_T, dim=1)

        embedding = torch.cat([s_max, s_avg, t_max, t_avg, master.squeeze(1)], dim=1)
        logits = self.out(self.drop(embedding))

        if return_embedding:
            return logits, embedding
        return logits

    @staticmethod
    def score_from_logits(logits: torch.Tensor) -> torch.Tensor:
        """Scalar score, higher = more synthetic.

        Fixed here so that no caller can accidentally invert the orientation
        that the calibration constants were fitted against.
        """
        # Class 0 is spoof and class 1 is bonafide - Item.target, the class
        # weights and the loss all agree - so spoof minus bonafide means
        # 'more synthetic'.  Inverting this silently swaps every verdict.
        return logits[:, 0] - logits[:, 1]


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)
