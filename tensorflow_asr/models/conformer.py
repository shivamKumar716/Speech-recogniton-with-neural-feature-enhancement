# Copyright 2020 Huy Le Nguyen (@usimarit)
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

import tensorflow as tf
import sys
from .activations import GLU
from .transducer import Transducer
from .layers.subsampling import VggSubsampling, Conv2dSubsampling
from .layers.positional_encoding import PositionalEncoding, PositionalEncodingConcat
from .layers.multihead_attention import MultiHeadAttention, RelPositionMultiHeadAttention
from ..utils.utils import shape_list

import numpy as np
L2 = tf.keras.regularizers.l2(1e-6)

t=0
class FFModule(tf.keras.layers.Layer):
    def __init__(self,
                 input_dim,
                 dropout=0.0,
                 fc_factor=0.5,
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name="ff_module",
                 **kwargs):
        super(FFModule, self).__init__(name=name, **kwargs)
        self.fc_factor = fc_factor
        self.ln = tf.keras.layers.LayerNormalization(
            name=f"{name}_ln",
            gamma_regularizer=kernel_regularizer,
            beta_regularizer=bias_regularizer
        )
        self.ffn1 = tf.keras.layers.Dense(
            4 * input_dim, name=f"{name}_dense_1",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.swish = tf.keras.layers.Activation(
            tf.keras.activations.swish, name=f"{name}_swish_activation")
        self.do1 = tf.keras.layers.Dropout(dropout, name=f"{name}_dropout_1")
        self.ffn2 = tf.keras.layers.Dense(
            input_dim, name=f"{name}_dense_2",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.do2 = tf.keras.layers.Dropout(dropout, name=f"{name}_dropout_2")
        self.res_add = tf.keras.layers.Add(name=f"{name}_add")

    def call(self, inputs, training=False, **kwargs):
        outputs = self.ln(inputs, training=training)
        outputs = self.ffn1(outputs, training=training)
        outputs = self.swish(outputs)
        outputs = self.do1(outputs, training=training)
        outputs = self.ffn2(outputs, training=training)
        outputs = self.do2(outputs, training=training)
        outputs = self.res_add([inputs, self.fc_factor * outputs])
        return outputs

    def get_config(self):
        conf = super(FFModule, self).get_config()
        conf.update({"fc_factor": self.fc_factor})
        conf.update(self.ln.get_config())
        conf.update(self.ffn1.get_config())
        conf.update(self.swish.get_config())
        conf.update(self.do1.get_config())
        conf.update(self.ffn2.get_config())
        conf.update(self.do2.get_config())
        conf.update(self.res_add.get_config())
        return conf


class MHSAModule(tf.keras.layers.Layer):
    def __init__(self,
                 head_size,
                 num_heads,
                 dropout=0.0,
                 mha_type="relmha",
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name="mhsa_module",
                 **kwargs):
        super(MHSAModule, self).__init__(name=name, **kwargs)
        self.ln = tf.keras.layers.LayerNormalization(
            name=f"{name}_ln",
            gamma_regularizer=kernel_regularizer,
            beta_regularizer=bias_regularizer
        )
        if mha_type == "relmha":
            self.mha = RelPositionMultiHeadAttention(
                name=f"{name}_mhsa",
                head_size=head_size, num_heads=num_heads,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer
            )
        elif mha_type == "mha":
            self.mha = MultiHeadAttention(
                name=f"{name}_mhsa",
                head_size=head_size, num_heads=num_heads,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer
            )
        else:
            raise ValueError("mha_type must be either 'mha' or 'relmha'")
        self.do = tf.keras.layers.Dropout(dropout, name=f"{name}_dropout")
        self.res_add = tf.keras.layers.Add(name=f"{name}_add")
        self.mha_type = mha_type

    def call(self, inputs, training=False, mask=None, **kwargs):
        inputs, pos = inputs  # pos is positional encoding
        outputs = self.ln(inputs, training=training)
        if self.mha_type == "relmha":
            outputs = self.mha([outputs, outputs, outputs, pos], training=training, mask=mask)
        else:
            outputs = outputs + pos
            outputs = self.mha([outputs, outputs, outputs], training=training, mask=mask)
        outputs = self.do(outputs, training=training)
        outputs = self.res_add([inputs, outputs])
        return outputs

    def get_config(self):
        conf = super(MHSAModule, self).get_config()
        conf.update({"mha_type": self.mha_type})
        conf.update(self.ln.get_config())
        conf.update(self.mha.get_config())
        conf.update(self.do.get_config())
        conf.update(self.res_add.get_config())
        return conf


class ConvModule(tf.keras.layers.Layer):
    def __init__(self,
                 input_dim,
                 kernel_size=32,
                 dropout=0.0,
                 depth_multiplier=1,
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name="conv_module",
                 **kwargs):
        super(ConvModule, self).__init__(name=name, **kwargs)
        self.ln = tf.keras.layers.LayerNormalization()
        self.pw_conv_1 = tf.keras.layers.Conv2D(
            filters=2 * input_dim, kernel_size=1, strides=1,
            padding="valid", name=f"{name}_pw_conv_1",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.glu = GLU(name=f"{name}_glu")
        self.dw_conv = tf.keras.layers.DepthwiseConv2D(
            kernel_size=(kernel_size, 1), strides=1,
            padding="same", name=f"{name}_dw_conv",
            depth_multiplier=depth_multiplier,
            depthwise_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.bn = tf.keras.layers.BatchNormalization(
            name=f"{name}_bn",
            gamma_regularizer=kernel_regularizer,
            beta_regularizer=bias_regularizer
        )
        self.swish = tf.keras.layers.Activation(
            tf.keras.activations.swish, name=f"{name}_swish_activation")
        self.pw_conv_2 = tf.keras.layers.Conv2D(
            filters=input_dim, kernel_size=1, strides=1,
            padding="valid", name=f"{name}_pw_conv_2",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.do = tf.keras.layers.Dropout(dropout, name=f"{name}_dropout")
        self.res_add = tf.keras.layers.Add(name=f"{name}_add")

    def call(self, inputs, training=False, **kwargs):
        outputs = self.ln(inputs, training=training)
        B, T, E = shape_list(outputs)
        outputs = tf.reshape(outputs, [B, T, 1, E])
        outputs = self.pw_conv_1(outputs, training=training)
        outputs = self.glu(outputs)
        outputs = self.dw_conv(outputs, training=training)
        outputs = self.bn(outputs, training=training)
        outputs = self.swish(outputs)
        outputs = self.pw_conv_2(outputs, training=training)
        outputs = tf.reshape(outputs, [B, T, E])
        outputs = self.do(outputs, training=training)
        outputs = self.res_add([inputs, outputs])
        return outputs

    def get_config(self):
        conf = super(ConvModule, self).get_config()
        conf.update(self.ln.get_config())
        conf.update(self.pw_conv_1.get_config())
        conf.update(self.glu.get_config())
        conf.update(self.dw_conv.get_config())
        conf.update(self.bn.get_config())
        conf.update(self.swish.get_config())
        conf.update(self.pw_conv_2.get_config())
        conf.update(self.do.get_config())
        conf.update(self.res_add.get_config())
        return conf


class ConformerBlock(tf.keras.layers.Layer):
    def __init__(self,
                 input_dim,
                 dropout=0.0,
                 fc_factor=0.5,
                 head_size=36,
                 num_heads=4,
                 mha_type="relmha",
                 kernel_size=32,
                 depth_multiplier=1,
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name="conformer_block",
                 **kwargs):
        super(ConformerBlock, self).__init__(name=name, **kwargs)
        self.ffm1 = FFModule(
            input_dim=input_dim, dropout=dropout,
            fc_factor=fc_factor, name=f"{name}_ff_module_1",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.mhsam = MHSAModule(
            mha_type=mha_type,
            head_size=head_size, num_heads=num_heads,
            dropout=dropout, name=f"{name}_mhsa_module",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.convm = ConvModule(
            input_dim=input_dim, kernel_size=kernel_size,
            dropout=dropout, name=f"{name}_conv_module",
            depth_multiplier=depth_multiplier,
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.ffm2 = FFModule(
            input_dim=input_dim, dropout=dropout,
            fc_factor=fc_factor, name=f"{name}_ff_module_2",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )
        self.ln = tf.keras.layers.LayerNormalization(
            name=f"{name}_ln",
            gamma_regularizer=kernel_regularizer,
            beta_regularizer=kernel_regularizer
        )

    def call(self, inputs, training=False, mask=None, **kwargs):
        inputs, pos = inputs  # pos is positional encoding
        outputs = self.ffm1(inputs, training=training, **kwargs)
        outputs = self.mhsam([outputs, pos], training=training, mask=mask, **kwargs)
        outputs = self.convm(outputs, training=training, **kwargs)
        outputs = self.ffm2(outputs, training=training, **kwargs)
        outputs = self.ln(outputs, training=training)
        return outputs

    def get_config(self):
        conf = super(ConformerBlock, self).get_config()
        conf.update(self.ffm1.get_config())
        conf.update(self.mhsam.get_config())
        conf.update(self.convm.get_config())
        conf.update(self.ffm2.get_config())
        conf.update(self.ln.get_config())
        return conf


class ConformerEncoder(tf.keras.Model):
    def __init__(self,
                 subsampling,
                 positional_encoding="sinusoid",
                 dmodel=144,
                 num_blocks=1,
                 mha_type="relmha",
                 head_size=36,
                 num_heads=4,
                 kernel_size=32,
                 depth_multiplier=1,
                 fc_factor=0.5,
                 dropout=0.0,
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name="conformer_encoder",
                 **kwargs):
        super(ConformerEncoder, self).__init__(name=name, **kwargs)
        subsampling_name = subsampling.pop("type", "conv2d")
        if subsampling_name == "vgg":
            subsampling_class = VggSubsampling
        elif subsampling_name == "conv2d":
            subsampling_class = Conv2dSubsampling
        else:
            raise ValueError("subsampling must be either  'conv2d' or 'vgg'")

        self.conv_subsampling = subsampling_class(
            **subsampling, name=f"{name}_subsampling",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )


        if positional_encoding == "sinusoid":
            self.pe = PositionalEncoding(name=f"{name}_pe")
        elif positional_encoding == "sinusoid_concat":
            self.pe = PositionalEncodingConcat(name=f"{name}_pe")
        elif positional_encoding == "subsampling":
            self.pe = tf.keras.layers.Activation("linear", name=f"{name}_pe")
        else:
            raise ValueError("positional_encoding must be either 'sinusoid' or 'subsampling'")

        self.linear = tf.keras.layers.Dense(
            dmodel, name=f"{name}_linear",
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer
        )

        self.do = tf.keras.layers.Dropout(dropout, name=f"{name}_dropout")
        self.gn = tf.keras.layers.GaussianNoise(0.0001, **kwargs)

        self.conformer_blocks = []
        for i in range(num_blocks):
            conformer_block = ConformerBlock(
                input_dim=dmodel,
                dropout=dropout,
                fc_factor=fc_factor,
                head_size=head_size,
                num_heads=num_heads,
                mha_type=mha_type,
                kernel_size=kernel_size,
                depth_multiplier=depth_multiplier,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer,
                name=f"{name}_block_{i}"
            )
            self.conformer_blocks.append(conformer_block)


    def call(self, inputs, training=False, mask=None,**kwargs):
        # input with shape [B, T, V1, V2]
        # output1 = self.addNoise(inputs)
        # print(self.steps)

        output1=self.gn(inputs)
        output1 = self.conv_subsampling(output1, training=training)
        output1 = self.linear(output1, training=training)
        pe = self.pe(output1)
        output1 = self.do(output1, training=training)
        for cblock in self.conformer_blocks:
            output1 = cblock([output1, pe], training=training, mask=mask, **kwargs)

        output2 = self.conv_subsampling(inputs, training=training)
        output2 = self.linear(output2, training=training)
        # pe = self.pe(output2)
        output2 = self.do(output2, training=training)
        # add a decaying weight
        # list3 = list(kwargs.items())
        # steps = list3[0][1]
        # steps_perEpoch = list3[1][1]
        # # for key,value in kwargs.items():
        # #     if key=='steps':
        # #         steps=value
        # #     elif key=='steps_per_epoch':
        # #         steps_perEpoch=value
        # ep=steps // steps_perEpoch + 1

        w=0
        ep=kwargs["ep"]
        w=float(ep)
        if w<=float(8):
            w=1-(w-1)/7
        else:
            w=float(0)

        # w=float(ep)

        mse=tf.keras.losses.MeanSquaredError(reduction=tf.keras.losses.Reduction.SUM)

        mse_loss=mse(output1,output2)

        return ((1-w)*output1+w*output2),mse_loss

    def get_config(self):
        conf = super(ConformerEncoder, self).get_config()
        conf.update(self.conv_subsampling.get_config())
        conf.update(self.linear.get_config())
        conf.update(self.do.get_config())
        conf.update(self.pe.get_config())
        for cblock in self.conformer_blocks:
            conf.update(cblock.get_config())
        return conf


class Conformer(Transducer):
    def __init__(self,
                 vocabulary_size: int,
                 encoder_subsampling: dict,
                 encoder_positional_encoding: str = "sinusoid",
                 encoder_dmodel: int = 144,
                 encoder_num_blocks: int = 16,
                 encoder_head_size: int = 36,
                 encoder_num_heads: int = 4,
                 encoder_mha_type: str = "relmha",
                 encoder_kernel_size: int = 32,
                 encoder_depth_multiplier: int = 1,
                 encoder_fc_factor: float = 0.5,
                 encoder_dropout: float = 0,
                 prediction_embed_dim: int = 512,
                 prediction_embed_dropout: int = 0,
                 prediction_num_rnns: int = 1,
                 prediction_rnn_units: int = 320,
                 prediction_rnn_type: str = "lstm",
                 prediction_rnn_implementation: int = 2,
                 prediction_layer_norm: bool = True,
                 prediction_projection_units: int = 0,
                 joint_dim: int = 1024,
                 joint_activation: str = "tanh",
                 kernel_regularizer=L2,
                 bias_regularizer=L2,
                 name: str = "conformer_transducer",
                 **kwargs):
        super(Conformer, self).__init__(
            encoder=ConformerEncoder(
                subsampling=encoder_subsampling,
                positional_encoding=encoder_positional_encoding,
                dmodel=encoder_dmodel,
                num_blocks=encoder_num_blocks,
                head_size=encoder_head_size,
                num_heads=encoder_num_heads,
                mha_type=encoder_mha_type,
                kernel_size=encoder_kernel_size,
                depth_multiplier=encoder_depth_multiplier,
                fc_factor=encoder_fc_factor,
                dropout=encoder_dropout,
                kernel_regularizer=kernel_regularizer,
                bias_regularizer=bias_regularizer
            ),
            vocabulary_size=vocabulary_size,
            embed_dim=prediction_embed_dim,
            embed_dropout=prediction_embed_dropout,
            num_rnns=prediction_num_rnns,
            rnn_units=prediction_rnn_units,
            rnn_type=prediction_rnn_type,
            rnn_implementation=prediction_rnn_implementation,
            layer_norm=prediction_layer_norm,
            projection_units=prediction_projection_units,
            joint_dim=joint_dim,
            joint_activation=joint_activation,
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer,
            name=name, **kwargs
        )
        self.dmodel = encoder_dmodel
        self.time_reduction_factor = self.encoder.conv_subsampling.time_reduction_factor

