"""trainer for RAM model, from [1]_.
Based on two implementations:
https://github.com/kevinzakka/recurrent-visual-attention
https://github.com/seann999/tensorflow_mnist_ram

.. [1] Mnih, Volodymyr, Nicolas Heess, and Alex Graves.
   "Recurrent models of visual attention."
   Advances in neural information processing systems. 2014.
   https://arxiv.org/abs/1406.6247
"""
import os
import sys
import time
from collections import namedtuple
import logging
from datetime import datetime

import tensorflow as tf
import numpy as np
from tqdm import tqdm
import attr

from . import ram

LossesTuple = namedtuple('LossesTuple', ['reinforce_loss',
                                         'baseline_loss',
                                         'action_loss',
                                         'hybrid_loss',
                                         ])

MeanLossTuple = namedtuple('MeanLossTuple', ['mn_reinforce_loss',
                                             'mn_baseline_loss',
                                             'mn_action_loss',
                                             'mn_hybrid_loss',
                                             ])


class Trainer:
    """Trainer object for training the RAM model"""
    def __init__(self,
                 config,
                 batch_size,
                 learning_rate,
                 epochs,
                 optimizer,
                 train_data,
                 val_data=None,
                 shuffle_each_epoch=True,
                 replicates=1,
                 restore=False,
                 checkpoint_prefix='ckpt',
                 save_log=False,
                 save_examples_every=None,
                 num_examples_to_save=None,
                 save_loss=False,
                 save_train_inds=False,
                 logger=None
                 ):
        """__init__ for Trainer

        Parameters
        ----------
        config : namedtuple
            as returned by ram.utils.parse_config.
        train_data : ram.dataset.Dataset
            Training data.
            Named tuple with fields:
                dataset : tensorflow Dataset object with images and labels / correct action
                num_samples : number of samples, int
            E.g., the MNIST training set,
            returned by calling ram.dataset.train.
        val_data : ram.dataset.Dataset
            Validation data. Default is None (in which case a validation score is not computed).
        """
        if train_data.num_samples % batch_size != 0:
            raise ValueError(f'Number of training samples, {train_data.num_samples}, '
                             f'is not evenly divisible by batch size, {config.train.batch_size}.\n'
                             f'This will cause an error when training network;'
                             f'please change either so that data.num_samples % config.train.batch_size == 0:')

        self.save_log = save_log  # if True, will create logfile in train method, using that method's result_dir arg
        if logger:
            self.logger = logger
        else:
            self.logger = logging.getLogger(__name__)
            self.logger.setLevel('INFO')
            self.logger.addHandler(logging.StreamHandler(sys.stdout))

        self.config = config
        self.logger.info(f'Trainer config: {config}')

        self.train_data = train_data.dataset
        self.logger.info(f'Training data: {self.train_data}')
        self.num_train_samples = train_data.num_samples
        self.logger.info(f'Number of samples in training data: {self.num_train_samples}')

        if val_data:
            self.val_data = val_data.dataset
            self.logger.info(f'Validation data: {self.val_data}')
            self.num_val_samples = val_data.num_samples
            self.logger.info(f'Number of samples in validation data: {self.num_val_samples}')
        else:
            self.val_data = None
            self.logger.info(f'Validation data: {self.val_data}')
            self.num_val_samples = None

        # hyperparams that will be common across replicates
        self.batch_size = batch_size
        self.logger.info(f'Batch size will be : {self.batch_size}')
        self.learning_rate = learning_rate
        self.logger.info(f'Initial learning rate will be : {self.learning_rate}')
        self.epochs = epochs
        self.logger.info(f'Will train for a maximum of {self.epochs} epochs')
        self.optimizer = optimizer
        self.logger.info(f'Optimizer : {self.optimizer}')

        self.shuffle_each_epoch = shuffle_each_epoch
        if self.shuffle_each_epoch:
            self.train_data = self.train_data.shuffle(buffer_size=self.num_train_samples,
                                                      reshuffle_each_iteration=True)

        # are we restoring a previous model?
        self.restore = restore
        # or are we training for a certain number of replicates?
        self.replicates = replicates

        self.checkpoint_prefix = checkpoint_prefix

        # below get replaced with an object when we start training
        self.checkpointer = None
        self.model = None

        # whether or not to save training data:
        # examples of output from certain epochs; loss;
        # indices of samples used for training sets
        self.save_examples_every = save_examples_every
        self.num_examples_to_save = num_examples_to_save
        self.save_loss = save_loss
        self.save_train_inds = save_train_inds
        self.data_dirs = {}

    @classmethod
    def from_config(cls, config, train_data, val_data=None, logger=None):
        if config.train.optimizer == 'momentum':
            optimizer = tf.train.MomentumOptimizer(momentum=config.train.momentum,
                                                   learning_rate=config.train.learning_rate)
        elif config.train.optimizer == 'sgd':
            optimizer = tf.train.GradientDescentOptimizer(learning_rate=config.train.learning_rate)
        elif config.train.optimizer == 'adam':
            optimizer = tf.train.AdamOptimizer(learning_rate=config.train.learning_rate,
                                               beta1=config.train.beta1,
                                               beta2=config.train.beta2,
                                               epsilon=config.train.epsilon)
        else:
            raise ValueError(f'optimizer type not recognized: {config.train.optimizer}')

        return cls(config=config,
                   batch_size=config.train.batch_size,
                   learning_rate=config.train.learning_rate,
                   epochs=config.train.epochs,
                   optimizer=optimizer,
                   train_data=train_data,
                   val_data=val_data,
                   shuffle_each_epoch=config.train.shuffle_each_epoch,
                   replicates=config.train.replicates,
                   restore=config.train.restore,
                   checkpoint_prefix=config.train.checkpoint_prefix,
                   save_log=config.misc.save_log,
                   save_examples_every=config.train.save_examples_every,
                   num_examples_to_save=config.train.num_examples_to_save,
                   save_loss=config.train.save_loss,
                   save_train_inds=config.train.save_train_inds,
                   logger=logger)

    def load_checkpoint(self, checkpoint_path):
        """loads model and optimizer from a checkpoint.
        Called when config.train.restore is True"""
        latest_checkpoint = tf.train.latest_checkpoint(checkpoint_path)
        if latest_checkpoint:
            self.logger.info(f'restoring model from latest checkpoint: {latest_checkpoint}')
            self.checkpointer.restore(latest_checkpoint)
        else:
            raise ValueError(f'no checkpoint found in checkpoint path: {checkpoint_path}')

    def save_checkpoint(self, checkpoint_path):
        """save model and optimizer to a checkpoint file"""
        self.checkpointer.save(file_prefix=checkpoint_path)

    def _name_and_create_data_dirs(self, results_dir):
        data_dirs = {}
        checkpoint_dir = os.path.join(results_dir, 'checkpoint')
        self.logger.info(f"Saving checkpoints in {checkpoint_dir}")
        if not os.path.isdir(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        data_dirs['checkpoint_path'] = os.path.join(checkpoint_dir,
                                                    self.checkpoint_prefix)

        if self.save_examples_every:
            self.logger.info(f"Will save examples every {self.save_examples_every} epochs")
            examples_dir = os.path.join(results_dir, 'examples')
            self.logger.info(f"Saving examples in {examples_dir}")
            if not os.path.isdir(examples_dir):
                os.makedirs(examples_dir)
            data_dirs['examples_dir'] = examples_dir
        else:
            self.logger.info("Will not save examples")

        if self.save_loss:
            loss_dir = os.path.join(results_dir, 'loss')
            self.logger.info(f"Saving loss in {loss_dir}")
            if not os.path.isdir(loss_dir):
                os.makedirs(loss_dir)
            data_dirs['loss_dir'] = loss_dir
        else:
            self.logger.info("Will not save record of loss")

        if self.save_train_inds:
            self.logger.info("Will save indices of samples from original training set")
            train_inds_dir = os.path.join(results_dir, 'train_inds')
            self.logger.info(f"Saving train_indices in {train_inds_dir}")
            if not os.path.isdir(train_inds_dir):
                os.makedirs(train_inds_dir)
            data_dirs['train_inds_dir'] = train_inds_dir
        else:
            self.logger.info("Will not save indices of samples from original training set")

        self.data_dirs = data_dirs

    def train(self,
              results_dir,
              checkpoint_path=None):
        """main train function

        Parameters
        ----------
        results_dir : str
            Path to directory where results are saved. If training a new model,
            new sub-directories will be created in results_dir.
        checkpoint_path : str
            Path to where checkpoints are saved. Only used when restoring models
            to train from a previous checkpoint. Default is None.

        Returns
        -------
        None
        """
        if not os.path.isdir(results_dir):
            raise NotADirectoryError(f'Directory to save result in not found: {results_dir}')

        if self.save_log:
            # if Trainer was called from cli, the logger will already have a FileHandler
            # but if not, we need to add one
            if logging.FileHandler not in [type(handler) for handler in self.logger.handlers]:
                # first need to make a log file to pass to Filehandler __init__
                timenow = datetime.now().strftime('%y%m%d_%H%M%S')
                logfile_name = os.path.join(results_dir,
                                            'logfile_from_ram_' + timenow + '.log')
                self.logger.addHandler(logging.FileHandler(logfile_name))
                self.logger.info('Logging results to {}'.format(results_dir))

        if self.restore:
            if checkpoint_path is None:
                raise ValueError('must specify checkpoint_path when restoring model')

            self.logger.info(f'restoring model and optimizer from checkpoint: {checkpoint_path}')
            self._name_and_create_data_dirs(results_dir)
            self.model = ram.RAM(batch_size=self.batch_size,
                                 **attr.asdict(self.config.model))
            self.checkpointer = tf.train.Checkpoint(optimizer=self.optimizer,
                                                    model=self.model,
                                                    optimizer_step=tf.train.get_or_create_global_step())
            self.load_checkpoint(checkpoint_path)
            self._train_one_model()
        else:
            for replicate in range(1, self.replicates + 1):
                self.logger.info(f"Starting replicate {replicate} of {self.replicates}\n")

                replicate_results_dir = os.path.join(results_dir, f'replicate_{replicate}')
                self.logger.info(f"Saving results in {replicate_results_dir}")
                if not os.path.isdir(replicate_results_dir):
                    os.makedirs(replicate_results_dir)

                self._name_and_create_data_dirs(replicate_results_dir)

                # apply model config
                self.model = ram.RAM(batch_size=self.batch_size,
                                     **attr.asdict(self.config.model))
                self.logger.info(f'Model that will be trained: {self.model}')
                self.checkpointer = tf.train.Checkpoint(optimizer=self.optimizer,
                                                        model=self.model,
                                                        optimizer_step=tf.train.get_or_create_global_step())
                self._train_one_model()

    def _train_one_model(self):
        """train one RAM model. Gets run once every replicate.
        """
        for epoch in range(1, self.epochs+1):
            self.logger.info(
                f'\nEpoch: {epoch}/{self.epochs} - learning rate: {self.learning_rate:.6f}'
            )

            # if this is an epoch on which we should save examples
            if self.save_examples_every:
                if epoch % self.save_examples_every == 0:
                    save_examples = True
                else:
                    save_examples = False
            else:
                save_examples = False

            mn_acc_dict, losses, mn_loss = self._train_one_epoch(current_epoch=epoch, save_examples=save_examples)
            # violating DRY by unpacking dict into vars,
            # because apparently format strings with dict keys blow up the PyCharm parser
            mn_acc = mn_acc_dict['mn_acc']
            if 'mn_val_acc' in mn_acc_dict:
                mn_val_acc = mn_acc_dict['mn_val_acc']
                self.logger.info(f'mean accuracy: {mn_acc}\n'
                                 f'mean validation accuracy: {mn_val_acc}\n'
                                 f'mean losses: {mn_loss}')
            else:
                self.logger.info(f'mean accuracy: {mn_acc}\nmean losses: {mn_loss}')
            self.save_checkpoint(checkpoint_path=self.data_dirs['checkpoint_path'])
            if self.save_loss:
                for loss_name, loss_arr in losses._asdict().items():
                    loss_filename = os.path.join(self.data_dirs['loss_dir'],
                                                 f'{loss_name}_epoch_{epoch}')
                    np.save(loss_filename, loss_arr)

    def _train_one_epoch(self, current_epoch, save_examples=False):
        """helper function that trains for one epoch.
        Called by trainer.train"""
        losses_reinforce = []
        losses_baseline = []
        losses_action = []
        losses_hybrid = []
        accs = []

        tic = time.time()

        if self.save_train_inds:
            train_inds = []

        if save_examples:
            locs = []
            fixations = []
            glimpses = []
            img_to_save = []
            pred = []
            num_examples_saved = 0

        with tqdm(total=self.num_train_samples) as progress_bar:
            batch = 0
            for img, lbl, batch_train_inds in self.train_data.batch(self.batch_size):
                if self.save_train_inds:
                    train_inds.extend(batch_train_inds)
                batch += 1

                out_t_minus_1 = self.model.reset()

                mus = []
                log_pis = []
                baselines = []

                with tf.GradientTape(persistent=True) as tape:
                    if save_examples:
                        if num_examples_saved < self.num_examples_to_save:
                            locs_t = []
                            fixations_t = []
                            glimpses_t = []

                    for t in range(self.model.glimpses):
                        out = self.model.step(img, out_t_minus_1.l_t, out_t_minus_1.h_t)

                        mus.append(out.mu)
                        baselines.append(out.b_t)
                        if save_examples:
                            if num_examples_saved < self.num_examples_to_save:
                                locs_t.append(out.l_t.numpy())
                                fixations_t.append(out.fixations)
                                glimpses_t.append(out.rho.numpy())
                        # determine probability of choosing location l_t, given
                        # distribution parameterized by mu (output of location network)
                        # and the constant standard deviation specified as a parameter.
                        # Assume both dimensions are independent
                        # 1. we get log probability from pdf for each dimension
                        # 2. we want the joint distribution which is the product of the pdfs
                        # 3. so we sum the log prob, since log(p(x) * p(y)) = log(p(x)) + log(p(y))
                        mu_distrib = tf.distributions.Normal(loc=out.mu,
                                                             scale=self.model.loc_std)
                        log_pi = mu_distrib.log_prob(value=out.l_t)
                        log_pi = tf.reduce_sum(log_pi, axis=1)
                        log_pis.append(log_pi)

                        out_t_minus_1 = out

                    # convert lists to tensors, reshape to (batch size x number of glimpses)
                    # for calculations below
                    baselines = tf.stack(baselines)
                    baselines = tf.squeeze(baselines)
                    baselines = tf.transpose(baselines, perm=[1, 0])

                    log_pis = tf.stack(log_pis)
                    log_pis = tf.squeeze(log_pis)
                    log_pis = tf.transpose(log_pis, perm=[1, 0])

                    # repeat column vector n times where n = glimpses
                    # calculate reward.
                    # Remember that action network output a_t becomes predictions at last time step
                    predicted = tf.argmax(
                        tf.nn.softmax(out.a_t),
                        axis=1, output_type=tf.int32)
                    R = tf.equal(predicted, lbl)
                    acc = np.sum(R.numpy()) / R.numpy().shape[-1] * 100
                    accs.append(acc)
                    R = tf.cast(R, dtype=tf.float32)
                    # reshape reward to (batch size x number of glimpses)
                    R = tf.expand_dims(R, axis=1)  # add axis
                    R = tf.tile(R, tf.constant([1, self.model.glimpses]))

                    # compute losses for differentiable modules
                    loss_action = tf.losses.softmax_cross_entropy(tf.one_hot(lbl, depth=self.model.num_classes),
                                                                  out.a_t)
                    loss_baseline = tf.losses.mean_squared_error(baselines, R)

                    # compute loss for REINFORCE algorithm
                    # summed over timesteps and averaged across batch
                    adjusted_reward = R - baselines
                    # note -log_pis below:
                    # REINFORCE uses gradient ascent so we minimize **negative** cost
                    loss_reinforce = tf.reduce_sum((-log_pis * adjusted_reward), axis=1)
                    loss_reinforce = tf.reduce_mean(loss_reinforce)

                    # sum up into hybrid loss
                    loss_hybrid = loss_action + loss_reinforce

                # apply reinforce loss **only** to location network
                lt_params = self.model.location_network.variables
                reinforce_grads = tape.gradient(loss_reinforce, lt_params)
                self.optimizer.apply_gradients(zip(reinforce_grads, lt_params),
                                               global_step=tf.train.get_or_create_global_step())

                # apply baseline loss to baseline network
                bt_params = self.model.baseline.variables
                baseline_grads = tape.gradient(loss_baseline, bt_params)
                self.optimizer.apply_gradients(zip(baseline_grads, bt_params),
                                               global_step=tf.train.get_or_create_global_step())

                # apply hybrid loss to glimpse network, core network, and action network
                params = [var for net in [self.model.glimpse_network,
                                          self.model.action_network,
                                          self.model.core_network]
                          for var in net.variables]
                hybrid_grads = tape.gradient(loss_hybrid, params)
                self.optimizer.apply_gradients(zip(hybrid_grads, params),
                                               global_step=tf.train.get_or_create_global_step())

                losses_reinforce.append(loss_reinforce.numpy())
                losses_baseline.append(loss_baseline.numpy())
                losses_action.append(loss_action.numpy())
                losses_hybrid.append(loss_hybrid.numpy())

                # deal with examples if we are saving them
                if save_examples:
                    # note we save the **first** n samples
                    if num_examples_saved < self.num_examples_to_save:
                        # stack so axis 0 is sample index, axis 1 is number of glimpses
                        locs_t = np.stack(locs_t, axis=1)
                        fixations_t = np.stack(fixations_t, axis=1)
                        glimpses_t = np.stack(glimpses_t, axis=1)

                        num_samples = locs_t.shape[0]

                        if num_examples_saved + num_samples <= self.num_examples_to_save:
                            locs.append(locs_t)
                            fixations.append(fixations_t)
                            glimpses.append(glimpses_t)
                            pred.append(predicted)
                            img_to_save.append(img)

                            num_examples_saved = num_examples_saved + num_samples

                        elif num_examples_saved + num_samples > self.num_examples_to_save:
                            num_needed = self.num_examples_to_save - num_examples_saved

                            locs.append(locs_t[:num_needed])
                            fixations.append(fixations_t[:num_needed])
                            glimpses.append(glimpses_t[:num_needed])
                            pred.append(predicted[:num_needed])
                            img_to_save.append(img[:num_needed])

                            num_examples_saved = num_examples_saved + num_needed

                toc = time.time()

                progress_bar.set_description(
                    (
                        "{:.1f}s - hybrid loss: {:.3f} - acc: {:.3f}".format(
                            (toc-tic), loss_hybrid, acc)
                    )
                )
                progress_bar.update(self.batch_size)

        if save_examples:
            for arr, stem in zip(
                    (locs, fixations, glimpses, img_to_save, pred),
                    ('locations', 'fixations', 'glimpses', 'images', 'predictions')
            ):
                arr = np.concatenate(arr)
                file = os.path.join(self.data_dirs['examples_dir'],
                                    f'{stem}_epoch_{current_epoch}')
                np.save(file=file, arr=arr)

        if self.save_train_inds:
            train_inds = np.asarray(train_inds)
            train_inds = np.squeeze(train_inds)
            train_inds_fname = f'train_inds_epoch_{current_epoch}'
            train_inds_fname = os.path.join(self.data_dirs['train_inds_dir'],
                                            train_inds_fname)
            np.save(train_inds_fname, train_inds)

        if self.val_data:
            self.logger.info('calculating validation accuracy')
            val_accs = []
            with tqdm(total=self.num_val_samples) as progress_bar:
                for img, lbl, batch_train_inds in self.val_data.batch(self.batch_size):
                    out_t_minus_1 = self.model.reset()
                    for t in range(self.model.glimpses):
                        out = self.model.step(img, out_t_minus_1.l_t, out_t_minus_1.h_t)
                        out_t_minus_1 = out

                    # Remember that action network output a_t becomes predictions at last time step
                    predicted = tf.argmax(
                        tf.nn.softmax(out.a_t),
                        axis=1, output_type=tf.int32)
                    val_acc = tf.equal(predicted, lbl)
                    val_acc = np.sum(val_acc.numpy()) / val_acc.numpy().shape[-1] * 100
                progress_bar.update(self.batch_size)
                val_accs.append(val_acc)
            mn_val_acc = np.mean(val_accs)

        mn_acc_dict = {'mn_acc': np.mean(accs)}
        if self.val_data:
            mn_acc_dict['mn_val_acc'] = mn_val_acc

        mn_loss_reinforce = np.asarray(losses_reinforce).mean()
        mn_loss_baseline = np.asarray(losses_baseline).mean()
        mn_loss_action = np.asarray(losses_action).mean()
        mn_losses_hybrid = np.asarray(losses_hybrid).mean()

        losses = LossesTuple(np.asarray(losses_reinforce),
                             np.asarray(losses_baseline),
                             np.asarray(losses_action),
                             np.asarray(losses_hybrid))
        mn_loss = MeanLossTuple(mn_loss_reinforce, mn_loss_baseline, mn_loss_action, mn_losses_hybrid)
        return mn_acc_dict, losses, mn_loss
