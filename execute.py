from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq2seq_model
import config

def read_train_data():
  source_path = config.id_file_train_enc
  target_path = config.id_file_train_dec

  data_set = [[] for _ in config._buckets]
  with tf.gfile.GFile(source_path, mode="r") as source_file:
    with tf.gfile.GFile(target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target:
        counter += 1
        if counter % 100000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = data_utils.trim([int(x) for x in source.split()])
        target_ids = data_utils.trim([int(x) for x in target.split()])
        target_ids.append(data_utils.EOS_ID)
        if len(source_ids) == 0 or len(target_ids) == 1:
          source, target = source_file.readline(), target_file.readline()
          continue
        for bucket_id, (source_size, target_size) in enumerate(config._buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            if config.useMovieData:
              data_set[bucket_id].append([source_ids, target_ids, 1.0])
            else:
              data_set[bucket_id].append([source_ids, target_ids])
            break
        source, target = source_file.readline(), target_file.readline()

  if config.useMovieData:
    data_set = merge_movie_data_into_train(data_set)

  return data_set

def merge_movie_data_into_train(data_set):
  movie_source_path = config.id_file_train_movie_enc
  movie_target_path = config.id_file_train_movie_dec

  with tf.gfile.GFile(movie_source_path, mode="r") as source_file:
    with tf.gfile.GFile(movie_target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target:
        counter += 1
        if counter % 100000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = [int(x) for x in source.split()]
        target_ids = [int(x) for x in target.split()]
        target_ids.append(data_utils.EOS_ID)
        for bucket_id, (source_size, target_size) in enumerate(config._buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids, target_ids, config.reduced_weight])
            break
        source, target = source_file.readline(), target_file.readline()
  return data_set

def read_val_data():
  data_set = [[] for _ in config._buckets]
  with tf.gfile.GFile(config.dev_enc_path, mode="r") as source_file:
    with tf.gfile.GFile(config.dev_dec_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target:
        counter += 1
        if counter % 100000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = data_utils.trim([int(x) for x in source.split()])
        target_ids = data_utils.trim([int(x) for x in target.split()])
        target_ids.append(data_utils.EOS_ID)
        if len(source_ids) == 0 or len(target_ids) == 1:
          source, target = source_file.readline(), target_file.readline()
          continue
        for bucket_id, (source_size, target_size) in enumerate(config._buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids, target_ids, 1.0])
            break
        source, target = source_file.readline(), target_file.readline()
  return data_set  # validation set

def bucket_stats(merged_train_set):
  num_buckets = len(config._buckets)   # 2 for debug, 5 for full
  # A list of bucket sizes, where bucket_size is num of examples in a bucket
  train_bucket_sizes = [len(merged_train_set[b]) for b in xrange(num_buckets)]
  train_total_size = float(sum(train_bucket_sizes))
  # Suppose we have four buckets with 30 items, 20 items, 10 items, and 40 items
  # which is a total of 100 examples.  The result is a CDF of the bucket ratios:
  # train_buckets_scale = [0.3, 0.5, 0.6, 1.0]
  bucket_scales = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                         for i in xrange(num_buckets)]
  return bucket_scales

def create_model(session):
  print("Building model...")
  start = time.time()
  model = seq2seq_model.Seq2SeqModel()
  print("Model built! Took {:.2f} seconds.".format(time.time() - start))

  ckpt = tf.train.get_checkpoint_state(config.working_directory)
  if ckpt and ckpt.model_checkpoint_path:
    cp = ckpt.model_checkpoint_path
    print("Restoring model using checkpoint parameters from %s" % cp)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    print("Creating model with fresh parameters.")
    session.run(tf.global_variables_initializer())

  return model

def train():

  bestPerp = 100000000

  with tf.Session(config=config_tf) as sess:
    model = create_model(sess)
    print("Creating RNN with %d units.\n" % (config.layer_size))

    if config.useTensorBoard:
      summary_op = tf.summary.merge_all()
      writer = tf.summary.FileWriter(config.logs_path, graph=tf.get_default_graph())
    else:
      summary_op = None

    merged_train_set = read_train_data()
    validation_set = read_val_data()
    bucket_scales = bucket_stats(merged_train_set)
    print("="*70)
    print("TRAINING")
    print("="*70)

    step_time, loss, current_step = 0.0, 0.0, 0
    previous_losses = []
    while True:
      # Choose a bucket according to data distribution. We pick a random number
      # in [0, 1] and use the corresponding interval in bucket_scales.
      random_number_01 = np.random.random_sample()
      bucket_id = min([i for i in xrange(len(bucket_scales))
                       if bucket_scales[i] > random_number_01])
      # Get a batch and make a step.
      start_time = time.time()

      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          merged_train_set, bucket_id)
      _, step_loss, _, tb_summary = model.step(sess, encoder_inputs, decoder_inputs,
                                   target_weights, bucket_id, False, summary_op)
      step_time += (time.time() - start_time) / config.steps_per_checkpoint
      loss += step_loss / config.steps_per_checkpoint
      current_step += 1

      # Once in a while, we save checkpoint, print statistics, and run evals.
      if current_step % config.steps_per_checkpoint == 0:
        if config.useTensorBoard:
          writer.add_summary(tb_summary, current_step)

        # Print statistics for the previous epoch.
        perplexity = math.exp(loss) if loss < 300 else float('inf')
        print ("global step %d learning rate %.4f step-time %.2f "
              "train perplexity %.2f" % (model.global_step.eval(),
              model.learning_rate.eval(), step_time, perplexity))
        # Annealing learning rate not necessary, using AdamOptimizer.
        # if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
        #   sess.run(model.learning_rate_decay_op)
        previous_losses.append(loss)
        step_time, loss = 0.0, 0.0
        totalPerp = 0
        # Run evals on development set and print their perplexity.
        for bucket_id in xrange(len(config._buckets)):
          if len(validation_set[bucket_id]) == 0:
            print("  eval: empty bucket %d" % (bucket_id))
            continue
          encoder_inputs, decoder_inputs, target_weights = model.get_batch(
              validation_set, bucket_id)
          _, eval_loss, outputs = model.step(sess, encoder_inputs, decoder_inputs,
                                       target_weights, bucket_id, True, summary_op)

          eval_ppx = math.exp(eval_loss) if eval_loss < 300 else float('inf')
          if eval_ppx:
            totalPerp += eval_ppx
          else:
            totalPerp = 10000000
          print("  Bucket %d: validation perplexity %.2f" % (bucket_id, eval_ppx))

        # Save checkpoint and zero timer and loss.
        if totalPerp < bestPerp:
          bestPerp = totalPerp
          print("BestPerp: %s. Saving model." % bestPerp)
          checkpoint_path = os.path.join(config.working_directory, "seq2seq.ckpt")
          model.saver.save(sess, checkpoint_path, global_step=model.global_step)

        sys.stdout.flush()

def test():
  with tf.Session() as sess:
    model = create_model(sess)
    model.batch_size = 1  # We decode one sentence at a time.

    vocab_word_to_id, vocab_list = data_utils.initialize_vocabulary(config.vocabPath)
    sentence = prompt_user("start")
    while sentence:   # What are token ids?
      token_ids = data_utils.sentence_to_token_ids(tf.compat.as_bytes(sentence), vocab_word_to_id)
      # Which bucket does it belong to?    print 'Length token ids:', len(token_ids)
      # print(' '.join([vocab_list[token_id] for token_id in token_ids]))
      potentialBuckets = [b for b in xrange(len(config._buckets))
                       if config._buckets[b][0] > len(token_ids)]
      if not potentialBuckets:
        sentence = prompt_user("long")
        continue
      bucket_id = min(potentialBuckets)
      # Get a 1-element batch to feed the sentence to the model.
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          {bucket_id: [(token_ids, [])]}, bucket_id)
      # Get output logits for the sentence.
      attention_where, _, output_logits = model.step(sess, encoder_inputs,
          decoder_inputs, target_weights, bucket_id, True, None)


      # print(np.shape(output_logits))         # This is a greedy decoder - outputs are just argmaxes of output_logits.
      outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
      # print('Untrimmed greedy outputs: %s' % ([tf.compat.as_str(vocab_list[output]) for output in outputs]))

      # If there is an EOS symbol in outputs, cut them at that point.
      if data_utils.EOS_ID in outputs:
        outputs = outputs[:outputs.index(data_utils.EOS_ID)]
      response = " ".join([tf.compat.as_str(vocab_list[output]) for output in outputs])
      sentence = prompt_user("next", response)

def prompt_user(phase, response=None):
  if phase == "start":
    print("Your query: ")
  elif phase == "next":
    print("Trumps response: ")
    print(response)
  elif phase == "long":
    print("Your input was too long.  Please try a shorter sentence.")

  sys.stdout.write("> ")
  sys.stdout.flush()
  sentence = sys.stdin.readline()
  return sentence

if __name__ == '__main__':
  if config.mode == 'train':
    # print("Preparing data in %s" % config.working_directory)
    data_utils.prepare_custom_data()
    config_tf = tf.ConfigProto()      # setup config to use BFC allocator
    config_tf.gpu_options.allocator_type = 'BFC'
    train()
  elif config.mode == 'test':
    data_utils.load_en()
    test()

