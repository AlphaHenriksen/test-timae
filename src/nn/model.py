
# References:
# MAE: https://github.com/facebookresearch/mae
# --------------------------------------------------------
import torch
from torch import nn, Tensor
import math
from src.nn.scaler import DAIN_Layer

import torch.distributions as pyd



class Embedder(nn.Module):
    def __init__(self,in_channels,d_model=64,kernel_size=3,stride=1,padding=1) -> None:
        super().__init__()
        self.conv = torch.nn.Conv1d(in_channels,d_model,kernel_size=kernel_size,stride=stride,padding=padding)
    
    def forward(self, src: Tensor) -> Tensor:
        """
        Args:
            src: Tensor, shape [batch_size, d_model, seq_len]

        Returns:
            output Tensor of shape [batch_size, seq_len, d_model]
        """
        return self.conv(src).permute(0,2,1)


class PositionalEncoding(nn.Module):

    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 100):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(1, max_len, d_model)
        pe[0, :, 0::2] = torch.sin(position * div_term)
        pe[0, :, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [batch_size, seq_len, d_model]

        Returns:
            output Tensor of shape [batch_size, seq_len, d_model]
        """

        x = x + self.pe[:,:x.size(1),:]
        return self.dropout(x)
    

class MaskedAutoencoder(nn.Module):
    """Masked Autoencoder with VanilaTransformer backbone for TimeSeries"""

    def __init__(
        self,
        in_chans=50,
        seq_len = 100,
        embed_dim=64,
        depth=2,
        num_heads=4,
        decoder_embed_dim=32,
        decoder_depth=2,
        decoder_num_heads=4,
        dropout = 0.1,
        norm_first = True,
        trunc_init = False,
        d_hid = 128,
        kernel_size = 3,
        stride = 1,
        padding =1,
        norm_layer=nn.LayerNorm,
        scale_mode = 'adaptive_scale',
        forecast_ratio = 0.25,
        forecast_steps = 10,
    ):
        super().__init__()

        self.seq_len = seq_len
        self.trunc_init = trunc_init
        self.embed_dim = embed_dim
        self.forecast_ratio = forecast_ratio
        self.forecast_steps = forecast_steps

        self.embedder = Embedder(in_chans,embed_dim,kernel_size=kernel_size,stride=stride,padding=padding)
        self.pos_encoder_e = PositionalEncoding(embed_dim,dropout,max_len=seq_len)
        self.pos_encoder_d = PositionalEncoding(decoder_embed_dim,dropout,max_len=seq_len)
        if scale_mode:
            self.scaler_layer = DAIN_Layer(scale_mode,input_dim=in_chans)
        else:
            self.scaler_layer = None



        self.blocks = nn.ModuleList(
            [
            
                nn.TransformerEncoderLayer(embed_dim, num_heads, d_hid, dropout,
                                            norm_first=norm_first,batch_first=True)
                
                for i in range(depth)
            ]
        )

        self.norm = norm_layer(embed_dim)

        self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))

        self.decoder_blocks = nn.ModuleList(
            [
                nn.TransformerEncoderLayer(decoder_embed_dim, decoder_num_heads, d_hid,
                                            dropout,norm_first=norm_first,batch_first=True)
                for i in range(decoder_depth)
            ]
        )

        self.decoder_norm = norm_layer(decoder_embed_dim)

        self.decoder_pred = nn.Linear(
            decoder_embed_dim, 
            in_chans ,# predict std + mean
            bias=True,
        )


        self.initialize_weights()

        print("model initialized")

    def initialize_weights(self):
        w = self.embedder.conv.weight.data
        if self.trunc_init:
            torch.nn.init.trunc_normal_(w)
            torch.nn.init.trunc_normal_(self.mask_token, std=0.02)
        else:
            torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))
            torch.nn.init.normal_(self.mask_token, std=0.02)

        # initialize nn.Linear and nn.LayerNorm
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            if self.trunc_init:
                nn.init.trunc_normal_(m.weight, std=0.02)
            else:
                torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def random_masking(self, x, mask_ratio,forecasting_ratio=0.25,forecast_steps=10):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))

        noise = torch.rand(N, L, device=x.device)  # noise in [0, 1]
        if forecasting_ratio:
            n_forecast_batches = int(N*forecasting_ratio)
            noise[-n_forecast_batches:,-(len_keep + forecast_steps):-forecast_steps ] -= 1.

        # sort noise for each sample
        ids_shuffle = torch.argsort(
            noise, dim=1
        )  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1)

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return x_masked, mask, ids_restore, ids_keep

    def forward_encoder(self, x, mask_ratio):
        # embed patches
        x = self.embedder(x)
        N, L, C = x.shape

        x = self.pos_encoder_e(x * math.sqrt(self.embed_dim))
        
        # masking: length -> length * mask_ratio
        x, mask, ids_restore, ids_keep = self.random_masking(x, mask_ratio,self.forecast_ratio,self.forecast_steps)

        

        # apply Transformer blocks
        for blk in self.blocks:
            x = blk(x)

        x = self.norm(x)

        return x, mask, ids_restore

    def forward_decoder(self, x, ids_restore):
        N = x.shape[0]

        # embed tokens
        x = self.decoder_embed(x)
        C = x.shape[-1]

        # append mask tokens to sequence
        mask_tokens = self.mask_token.repeat(N, self.seq_len - x.shape[1], 1)

        x_ = torch.cat([x[:, :, :], mask_tokens], dim=1)  # no cls token
        # x_ = x_.view([N, self.seq_len, C])
        
        x_ = torch.gather(
            x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x_.shape[2])
        )  # unshuffle

        x = x_.view([N, self.seq_len, C])
        # append cls token

        x = self.pos_encoder_d(x)

        # apply Transformer blocks
        for blk in self.decoder_blocks:
            x = blk(x)

        x = self.decoder_norm(x)

        # predictor projection
        x = self.decoder_pred(x)

        return x

    def forward_loss(self, x, pred, mask,latent,forecasting_ratio=0.25,forecast_steps=10):
        """
        x: [N, W, L]
        pred: [N, L, W]
        mask: [N, W], 0 is keep, 1 is remove,
        """
        
        loss = torch.abs(pred - x.permute(0,2,1))
        loss = torch.nan_to_num(loss,nan=10,posinf=10,neginf=10)
        loss = torch.clamp(loss,max=10)
        
        loss = loss.mean(dim=-1)  # [N, L], mean loss per timestamp
        
        if forecasting_ratio:
            n_forecast_batches = int(pred.shape[0]*forecasting_ratio)
            loss_forecast = loss[-n_forecast_batches:,:]
            mask_forecast = mask[-n_forecast_batches:,:]

            loss = loss[:-n_forecast_batches,:]
            mask = mask[:-n_forecast_batches,:]

            forecast_loss = loss_forecast[:,-forecast_steps:].sum() / forecast_steps
            backcast_loss = (loss_forecast * mask_forecast)[:,:-forecast_steps].sum() / (mask_forecast.sum() - forecast_steps)      
        else:
            forecast_loss = 0
            backcast_loss = 0

        inv_mask = (mask -1 ) ** 2
        loss_removed = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
        loss_seen = (loss * inv_mask).sum() / inv_mask.sum()  # mean loss on seen patches

     
        return loss_removed , loss_seen, forecast_loss, backcast_loss
    

    def forward(self, x, mask_ratio=0.75):
        if self.scaler_layer != None:
            x_ = self.scaler_layer(x)
        else:
            x_ = x

        latent, mask, ids_restore = self.forward_encoder(x_, mask_ratio)
        pred = self.forward_decoder(latent, ids_restore)  # [N, L, p*p*3]
        loss = self.forward_loss(x, pred, mask,latent,self.forecast_ratio,self.forecast_steps)
        return loss, pred, mask

    def predict(self,x,pred_samples=5):
        with torch.no_grad():

            if self.scaler_layer != None:
                x = self.scaler_layer(x)

            N,W,L = x.shape
            x = self.embedder(x[:,:,:-pred_samples])
            x = self.pos_encoder_e(x * math.sqrt(self.embed_dim))

            for blk in self.blocks:
                x = blk(x)

            x = self.norm(x) 
            x = self.decoder_embed(x)
            z = x.clone()

            if pred_samples:
                mask_tokens = self.mask_token.repeat(N, pred_samples, 1)

                x = torch.cat([x[:, :, :], mask_tokens], dim=1)  # no cls token

            x = self.pos_encoder_d(x)

            for blk in self.decoder_blocks:
                x = blk(x)

            x = self.decoder_norm(x)

            x = self.decoder_pred(x)

        return x,z




# import torch
# x = torch.rand((5,50,100))
# timae = MaskedAutoencoder()
# pred = timae(x)

# print(pred[1].shape,pred[2].shape,x.shape)
# print(f'Loss : {pred[0]}')