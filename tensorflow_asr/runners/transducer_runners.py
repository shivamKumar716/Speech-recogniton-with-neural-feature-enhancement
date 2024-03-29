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

import os
import tensorflow as tf
import math
import numpy as np
import sys
from ..configs.config import RunningConfig
from ..optimizers.accumulation import GradientAccumulation
from .base_runners import BaseTrainer
from ..losses.rnnt_losses import rnnt_loss
from ..models.transducer import Transducer
from ..featurizers.text_featurizers import TextFeaturizer
from ..utils.utils import get_reduced_length

import sys

class TransducerTrainer(BaseTrainer):
    def __init__(self,
                 config: RunningConfig,
                 text_featurizer: TextFeaturizer,
                 strategy: tf.distribute.Strategy = None):
        self.text_featurizer = text_featurizer
        super(TransducerTrainer, self).__init__(config, strategy=strategy)

    def set_train_metrics(self):
        self.train_metrics = {
            "transducer_loss": tf.keras.metrics.Mean("train_transducer_loss", dtype=tf.float32)
        }

    def set_eval_metrics(self):
        self.eval_metrics = {
            "transducer_loss": tf.keras.metrics.Mean("eval_transducer_loss", dtype=tf.float32)
        }

    def save_model_weights(self):
        self.model.save_weights(os.path.join(self.config.outdir, "latest.h5"))

    #@tf.function(experimental_relax_shapes=True)
    def _train_step(self, batch):
        features, input_length, labels, label_length, prediction, prediction_length = batch
        ep = self.steps // self.train_steps_per_epoch + 1
        kwargs={}
        kwargs.update(ep=ep)


        with tf.GradientTape() as tape:
            logits, mse_loss = self.model([features, input_length, prediction, prediction_length,ep], training=True
                                        ,**kwargs)
            tape.watch(logits)
            per_train_loss = rnnt_loss(
                logits=logits, labels=labels, label_length=label_length,
                logit_length=get_reduced_length(input_length, self.model.time_reduction_factor),
                blank=self.text_featurizer.blank
            )
            lambda_ = 0
            if float(ep)<=8:
                lambda_=1-float(ep-1)/7


            per_train_loss += lambda_*mse_loss
            train_loss = tf.nn.compute_average_loss(per_train_loss,
                                                    global_batch_size=self.global_batch_size)

        gradients = tape.gradient(train_loss, self.model.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.model.trainable_variables))

        self.train_metrics["transducer_loss"].update_state(per_train_loss)

    @tf.function(experimental_relax_shapes=True)
    def _eval_step(self, batch):
        features, input_length, labels, label_length, prediction, prediction_length = batch
        ep = self.steps // self.train_steps_per_epoch + 1
        kwargs={}
        kwargs.update(ep=ep)
        logits = self.model([features, input_length, prediction, prediction_length,ep], training=False,**kwargs)[0]
        eval_loss = rnnt_loss(
            logits=logits, labels=labels, label_length=label_length,
            logit_length=get_reduced_length(input_length, self.model.time_reduction_factor),
            blank=self.text_featurizer.blank
        )

        self.eval_metrics["transducer_loss"].update_state(eval_loss)

    def compile(self,
                model: Transducer,
                optimizer: any,
                max_to_keep: int = 10):
        with self.strategy.scope():
            self.model = model
            self.optimizer = tf.keras.optimizers.get(optimizer)
        self.create_checkpoint_manager(max_to_keep, model=self.model, optimizer=self.optimizer)


class TransducerTrainerGA(TransducerTrainer):
    """ Transducer Trainer that uses Gradients Accumulation """

    @tf.function
    def _train_function(self, iterator):
        for _ in range(self.config.accumulation_steps):
            batch = next(iterator)
            self.strategy.run(self._train_step, args=(batch,))
        self.strategy.run(self._apply_gradients, args=())

    @tf.function
    def _apply_gradients(self):
        self.optimizer.apply_gradients(
            zip(self.accumulation.gradients, self.model.trainable_variables))
        self.accumulation.reset()

    @tf.function(experimental_relax_shapes=True)
    def _train_step(self, batch):
        features, input_length, labels, label_length, prediction, prediction_length = batch

        with tf.GradientTape() as tape:
            logits = self.model([features, input_length, prediction, prediction_length], training=True)
            tape.watch(logits)
            per_train_loss = rnnt_loss(
                logits=logits, labels=labels, label_length=label_length,
                logit_length=get_reduced_length(input_length, self.model.time_reduction_factor),
                blank=self.text_featurizer.blank
            )
            train_loss = tf.nn.compute_average_loss(
                per_train_loss,
                global_batch_size=self.global_batch_size
            )

        gradients = tape.gradient(train_loss, self.model.trainable_variables)
        self.accumulation.accumulate(gradients)
        self.train_metrics["transducer_loss"].update_state(per_train_loss)

    def compile(self,
                model: Transducer,
                optimizer: any,
                max_to_keep: int = 10):
        with self.strategy.scope():
            self.model = model
            self.optimizer = tf.keras.optimizers.get(optimizer)
        self.create_checkpoint_manager(max_to_keep, model=self.model, optimizer=self.optimizer)
        self.accumulation = GradientAccumulation(self.model.trainable_variables)
