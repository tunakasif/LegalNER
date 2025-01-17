import functools
from pathlib import Path
import json
import numpy as np
import tensorflow as tf
import utilities
import logging
import sys


class LSTM_CRF:
    def __str__(self):
        return "LSTM_CRF"

    def __init__(self, embedding="glove", settings='data.json'):
        """
        Hyperparameters for LSTM - CRF model
        :param embedding: (String) embedding mode (glove, m2v, hybrid)
        """
        embeddings = ("glove", "m2v", "hybrid")
        self.embed_flag = 0
        if embedding in embeddings:
            self.embed_flag = embeddings.index(embedding)
        print(" ======== " + embeddings[self.embed_flag] + " + LSTM - CRF ========")
        self.results_dir = 'lc_' + embedding + "_results"
        self.dataset_dir = ''
        self.data_dir = ''
        with open(settings) as json_file:
            data = json.load(json_file)
            self.data_dir = data['DATA_DIR']
            self.dataset_dir = data['DATASET_DIR']

        # Params
        self.params = {
            'dim': 300,
            'dropout': 0.5,
            'num_oov_buckets': 1,
            'epochs': 25,
            'batch_size': 20,
            'buffer': 15000,
            'lstm_size': 100,
            'words': str(Path(self.data_dir, 'vocab.words.txt')),
            'chars': str(Path(self.data_dir, 'vocab.chars.txt')),
            'tags': str(Path(self.data_dir, 'vocab.tags.txt')),
            'glove': str(Path(self.data_dir, 'glove.npz')),
            'm2v': str(Path(self.data_dir, 'morph2vec.npz'))
        }

        # Logging
        Path(self.results_dir).mkdir(exist_ok=True)
        tf.compat.v1.logging.set_verbosity(logging.INFO)
        handlers = [
            logging.FileHandler(str(Path(self.results_dir, 'lc.log'))),
            logging.StreamHandler(sys.stdout)
        ]
        logging.getLogger('tensorflow').handlers = handlers

        Path(self.results_dir).mkdir(exist_ok=True)
        with Path(self.results_dir, 'params.json').open('w') as f:
            json.dump(self.params, f, indent=4, sort_keys=True)

# ======================================================================================================================
    def get_datapath(self, name, type):
        file = '{}.' + type + '.txt'
        return str(Path(self.dataset_dir, file.format(name)))

# ======================================================================================================================
    def model_fn(self, features, labels, mode):
        # For serving features are a bit different
        if isinstance(features, dict):
            features = features['words'], features['nwords']

        # Read vocabs and inputs
        dropout = self.params['dropout']
        words, nwords = features
        training = (mode == tf.estimator.ModeKeys.TRAIN)
        vocab_words = tf.contrib.lookup.index_table_from_file(
                      self.params['words'], num_oov_buckets=self.params['num_oov_buckets'])
        with Path(self.params['tags']).open(encoding="utf-8") as f:
            indices = [idx for idx, tag in enumerate(f) if tag.strip() != 'O']
            num_tags = len(indices) + 1

        # Word Embeddings
        word_ids = vocab_words.lookup(words)
        glove = np.load(self.params['glove'])['embeddings']  # np.array
        m2v = np.load(self.params['m2v'])['embeddings']      # np.array
        variable_g = np.vstack([glove, [[0.] * self.params['dim']]])
        variable_m = np.vstack([m2v, [[0.] * self.params['dim']]])
        variable = variable_g
        if self.embed_flag is 1:
            variable = variable_m
        elif self.embed_flag is 2:
            variable = np.vstack([variable_g, variable_m])
        variable = tf.Variable(variable, dtype=tf.float32, trainable=False)
        embeddings = tf.nn.embedding_lookup(variable, word_ids)

        # LSTM
        t = tf.transpose(embeddings, perm=[1, 0, 2])  # Need time-major
        lstm_cell_fw = tf.contrib.rnn.LSTMBlockFusedCell(self.params['lstm_size'])
        lstm_cell_bw = tf.contrib.rnn.LSTMBlockFusedCell(self.params['lstm_size'])
        lstm_cell_bw = tf.contrib.rnn.TimeReversedFusedRNN(lstm_cell_bw)
        output_fw, _ = lstm_cell_fw(t, dtype=tf.float32, sequence_length=nwords)
        output_bw, _ = lstm_cell_bw(t, dtype=tf.float32, sequence_length=nwords)
        output = tf.concat([output_fw, output_bw], axis=-1)
        output = tf.transpose(output, perm=[1, 0, 2])
        output = tf.layers.dropout(output, rate=dropout, training=training)

        # CRF
        logits = tf.layers.dense(output, num_tags)
        crf_params = tf.get_variable("crf", [num_tags, num_tags], dtype=tf.float32)
        pred_ids, _ = tf.contrib.crf.crf_decode(logits, crf_params, nwords)

        if mode == tf.estimator.ModeKeys.PREDICT:
            # Predictions
            reverse_vocab_tags = tf.contrib.lookup.index_to_string_table_from_file(self.params['tags'])
            pred_strings = reverse_vocab_tags.lookup(tf.to_int64(pred_ids))
            predictions = {
                'pred_ids': pred_ids,
                'tags': pred_strings
            }
            return tf.estimator.EstimatorSpec(mode, predictions=predictions)
        else:
            # Loss
            vocab_tags = tf.contrib.lookup.index_table_from_file(self.params['tags'])
            tags = vocab_tags.lookup(labels)
            log_likelihood, _ = tf.contrib.crf.crf_log_likelihood(
                logits, tags, nwords, crf_params)
            loss = tf.reduce_mean(-log_likelihood)

            # Metrics
            weights = tf.sequence_mask(nwords)
            metrics = {
                'acc': tf.metrics.accuracy(tags, pred_ids, weights),
                'precision': utilities.precision(tags, pred_ids, num_tags, indices, weights),
                'recall': utilities.recall(tags, pred_ids, num_tags, indices, weights),
                'f1': utilities.f1(tags, pred_ids, num_tags, indices, weights),
            }
            for metric_name, op in metrics.items():
                tf.summary.scalar(metric_name, op[1])

            if mode == tf.estimator.ModeKeys.EVAL:
                return tf.estimator.EstimatorSpec(mode, loss=loss, eval_metric_ops=metrics)

            elif mode == tf.estimator.ModeKeys.TRAIN:
                train_op = tf.train.AdamOptimizer().minimize(
                    loss, global_step=tf.train.get_or_create_global_step())
                return tf.estimator.EstimatorSpec(mode, loss=loss, train_op=train_op)

# ======================================================================================================================
    @staticmethod
    def parse_fn(line_words, line_tags):
        # Encode in Bytes for TF
        words = [w.encode() for w in line_words.strip().split()]
        tags = [t.encode() for t in line_tags.strip().split()]
        assert len(words) == len(tags), "Words and tags lengths don't match"
        return (words, len(words)), tags

# ======================================================================================================================
    @staticmethod
    def generator_fn(words, tags):
        with Path(words).open('r', encoding="utf-8") as f_words, Path(tags).open('r', encoding="utf-8") as f_tags:
            for line_words, line_tags in zip(f_words, f_tags):
                yield LSTM_CRF.parse_fn(line_words, line_tags)

# ======================================================================================================================
    @staticmethod
    def input_fn(words, tags, params=None, shuffle_and_repeat=False):
        params = params if params is not None else {}
        shapes = (([None], ()), [None])
        types = ((tf.string, tf.int32), tf.string)
        defaults = (('<pad>', 0), 'O')

        dataset = tf.data.Dataset.from_generator(
            functools.partial(LSTM_CRF.generator_fn, words, tags),
            output_shapes=shapes, output_types=types)

        if shuffle_and_repeat:
            dataset = dataset.shuffle(params['buffer']).repeat(params['epochs'])

        dataset = (dataset
                   .padded_batch(params.get('batch_size', 20), shapes, defaults)
                   .prefetch(1))
        return dataset

# ======================================================================================================================
    @staticmethod
    def predict_input_fn(sample):
        with Path(sample).open('r', encoding="utf-8") as text:
            for line in text:
                # Words
                words = [w.encode() for w in line.strip().split()]
                nwords = len(words)

                # Wrapping in Tensors
                words = tf.constant([words], dtype=tf.string)
                nwords = tf.constant([nwords], dtype=tf.int32)

                yield (words, nwords), None
