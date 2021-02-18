#!/usr/bin/env python
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import json
import os

import numpy as np
from sklearn.metrics import roc_auc_score, accuracy_score, \
    average_precision_score
from scipy.special import softmax
import torch

from scvi.data import read_h5ad, read_loom, setup_anndata
from scvi.model import SCVI
from scvi.external import SOLO

from pytorch_lightning.callbacks.early_stopping import EarlyStopping
import umap


from .utils import knn_smooth_pred_class


'''
solo.py

Simulate doublets, train a VAE, and then a classifier on top.
'''


###############################################################################
# main
###############################################################################


def main():
    usage = 'solo'
    parser = ArgumentParser(usage, formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument(dest='model_json_file',
                        help='json file to pass VAE parameters')
    parser.add_argument(dest='data_path',
                        help='path to h5ad, loom or 10x directory containing cell by genes counts')
    parser.add_argument('--set-reproducible-seed', dest='reproducible_seed',
                        default=None, type=int,
                        help='Reproducible seed, give an int to set seed')
    parser.add_argument('-d', dest='doublet_depth',
                        default=2., type=float,
                        help='Depth multiplier for a doublet relative to the \
                        average of its constituents')
    parser.add_argument('-g', dest='gpu',
                        default=True, action='store_true',
                        help='Run on GPU')
    parser.add_argument('-a', dest='anndata_output',
                        default=False, action='store_true',
                        help='output modified anndata object with solo scores \
                        Only works for anndata')
    parser.add_argument('-o', dest='out_dir',
                        default='solo_out')
    parser.add_argument('-r', dest='doublet_ratio',
                        default=2., type=float,
                        help='Ratio of doublets to true \
                        cells')
    parser.add_argument('-s', dest='seed',
                        default=None, help='Path to previous solo output  \
                        directory. Seed VAE models with previously \
                        trained solo model. Directory structure is assumed to \
                        be the same as solo output directory structure. \
                        should at least have a vae.pt a pickled object of \
                        vae weights and a latent.npy an np.ndarray of the \
                        latents of your cells.')
    parser.add_argument('-k', dest='known_doublets',
                        help='Experimentally defined doublets tsv file. \
                        Should be a single column of True/False. True \
                        indicates the cell is a doublet. No header.',
                        type=str)
    parser.add_argument('-t', dest='doublet_type', help='Please enter \
                        multinomial, average, or sum',
                        default='multinomial',
                        choices=['multinomial', 'average', 'sum'])
    parser.add_argument('-e', dest='expected_number_of_doublets',
                        help='Experimentally expected number of doublets',
                        type=int, default=None)
    parser.add_argument('-p', dest='plot',
                        default=False, action='store_true',
                        help='Plot outputs for solo')
    parser.add_argument('-l', dest='normal_logging',
                        default=False, action='store_true',
                        help='Logging level set to normal (aka not debug)')
    parser.add_argument('--random_size', dest='randomize_doublet_size',
                        default=False,
                        action='store_true',
                        help='Sample depth multipliers from Unif(1, \
                        DoubletDepth) \
                        to provide a diversity of possible doublet depths.'
                        )
    args = parser.parse_args()

    model_json_file = args.model_json_file
    data_path = args.data_path
    if args.gpu and not torch.cuda.is_available():
        args.gpu = torch.cuda.is_available()
        print('Cuda is not available, switching to cpu running!')

    if not os.path.isdir(args.out_dir):
        os.mkdir(args.out_dir)

    if args.reproducible_seed is not None:
        torch.manual_seed(args.reproducible_seed)
        np.random.seed(args.reproducible_seed)
    ##################################################
    # data

    # read loom/anndata
    data_ext = os.path.splitext(data_path)[-1]
    if data_ext == '.loom':
        scvi_data = read_loom(data_path)
    elif data_ext == '.h5ad':
        scvi_data = read_h5ad(data_path)
    else:
        msg = f'{data_path} is not a recognized format.\n'
        msg += 'must be one of {h5ad, loom}'
        raise TypeError(msg)

    num_cells, num_genes = scvi_data.X.shape

    # check for parameters
    if not os.path.exists(model_json_file):
        raise FileNotFoundError(f'{model_json_file} does not exist.')
    # read parameters
    with open(model_json_file, 'r') as model_json_open:
        params = json.load(model_json_open)

    # set VAE params
    vae_params = {}
    for par in ['n_hidden', 'n_latent', 'n_layers', 'dropout_rate',
                'ignore_batch']:
        if par in params:
            vae_params[par] = params[par]
    vae_params['n_batch'] = 0 if params.get(
        'ignore_batch', False) else scvi_data.n_batches

    # training parameters
    batch_size = params.get('batch_size', 128)
    valid_pct = params.get('valid_pct', 0.1)
    learning_rate = params.get('learning_rate', 1e-3)
    stopping_params = {'patience': params.get('patience', 20), 'min_delta': 0}

    # protect against single example batch
    while num_cells % batch_size == 1:
        batch_size = int(np.round(1.25*batch_size))
        print('Increasing batch_size to %d to avoid single example batch.' % batch_size)

    ##################################################
    # SCVI
    setup_anndata(scvi_data)
    vae = SCVI(scvi_data,
               gene_likelihood='nb',
               log_variational=True,
               batch_size=batch_size,
               **vae_params)

    if args.seed:
        vae.load(os.path.join(args.seed, 'vae.pt'), use_gpu=args.gpu)
    else:
        scvi_callbacks = []
        scvi_callbacks += [EarlyStopping(
                monitor='reconstruction_loss_validation',
                mode='min',
                **stopping_params
                )]
        vae.train(max_epochs=500,
                  validation_size=valid_pct,
                  check_val_every_n_epoch=1,
                  callbacks=scvi_callbacks
                  )
        # save VAE
        vae.save(os.path.join(args.out_dir, 'vae.pt'))

    latent = vae.get_latent_representation()
    # save latent representation
    np.save(os.path.join(args.out_dir, 'latent.npy'),
            latent.astype('float32'))

    ##################################################
    # classifier

    # model
    # todo add doublet ratio
    solo = SOLO.from_scvi_model(vae)
    solo.train(500,
               lr=learning_rate,
               train_size=.8,
               validation_size=.1,
               check_val_every_n_epoch=1,
               early_stopping_patience=20)
    if learning_rate > 1e-4:
        solo.train(200, lr=1e-4, train_size=.8, validation_size=.1,
                   check_val_every_n_epoch=1,
                   early_stopping_patience=20)

    solo.save(os.path.join(args.out_dir, 'classifier.pt'))

    logit_predictions = solo.predict()

    is_doublet_known = solo.adata.obs._solo_doub_sim == 'doublet'
    is_doublet_pred = np.argmin(logit_predictions, axis=1)

    validation_is_doublet_known = is_doublet_known[solo.validation_indices]
    validation_is_doublet_pred = is_doublet_pred[solo.validation_indices]
    training_is_doublet_known = is_doublet_known[solo.train_indices]
    training_is_doublet_pred = is_doublet_pred[solo.train_indices]
    test_is_doublet_known = is_doublet_known[solo.test_indices]
    test_is_doublet_pred = is_doublet_pred[solo.test_indices]

    valid_as = accuracy_score(validation_is_doublet_known, validation_is_doublet_pred)
    valid_roc = roc_auc_score(validation_is_doublet_known, validation_is_doublet_pred)
    valid_ap = average_precision_score(validation_is_doublet_known, validation_is_doublet_pred)

    train_as = accuracy_score(training_is_doublet_known, training_is_doublet_pred)
    train_roc = roc_auc_score(training_is_doublet_known, training_is_doublet_pred)
    train_ap = average_precision_score(training_is_doublet_known, training_is_doublet_pred)

    test_as = accuracy_score(test_is_doublet_known, test_is_doublet_pred)
    test_roc = roc_auc_score(test_is_doublet_known, test_is_doublet_pred)
    test_ap = average_precision_score(test_is_doublet_known, test_is_doublet_pred)

    print(f'Training results')
    print(f'AUROC: {train_roc}, Accuracy: {train_as}, Average precision: {train_ap}')

    print(f'Validation results')
    print(f'AUROC: {valid_roc}, Accuracy: {valid_as}, Average precision: {valid_ap}')

    print(f'Test results')
    print(f'AUROC: {test_roc}, Accuracy: {test_as}, Average precision: {test_ap}')

    # write predictions
    # softmax predictions
    softmax_predictions = softmax(logit_predictions, axis=1)
    doublet_score = softmax_predictions[:, 0]
    np.save(os.path.join(args.out_dir, 'no_updates_softmax_scores.npy'), doublet_score[:num_cells])
    np.savetxt(os.path.join(args.out_dir, 'no_updates_softmax_scores.csv'), doublet_score[:num_cells], delimiter=",")
    np.save(os.path.join(args.out_dir, 'no_updates_softmax_scores_sim.npy'), doublet_score[num_cells:])

    # logit predictions
    logit_doublet_score = logit_predictions[:, 1]
    np.save(os.path.join(args.out_dir, 'logit_scores.npy'), logit_doublet_score[:num_cells])
    np.savetxt(os.path.join(args.out_dir, 'logit_scores.csv'), logit_doublet_score[:num_cells], delimiter=",")
    np.save(os.path.join(args.out_dir, 'logit_scores_sim.npy'), logit_doublet_score[num_cells:])


    # update threshold as a function of Solo's estimate of the number of
    # doublets
    # essentially a log odds update
    # TODO put in a function
    diff = np.inf
    counter_update = 0
    solo_scores = doublet_score[:num_cells]
    logit_scores = logit_doublet_score[:num_cells]
    d_s = (args.doublet_ratio / (args.doublet_ratio + 1))
    while (diff > .01) | (counter_update < 5):

        # calculate log odds calibration for logits
        d_o = np.mean(solo_scores)
        c = np.log(d_o/(1-d_o)) - np.log(d_s/(1-d_s))

        # update solo scores
        solo_scores = 1 / (1+np.exp(-(logit_scores + c)))

        # update while conditions
        diff = np.abs(d_o - np.mean(solo_scores))
        counter_update += 1

    np.save(os.path.join(args.out_dir, 'softmax_scores.npy'),
            solo_scores)
    np.savetxt(os.path.join(args.out_dir, 'softmax_scores.csv'),
               solo_scores, delimiter=",")

    if args.expected_number_of_doublets is not None:
        k = len(solo_scores) - args.expected_number_of_doublets
        if args.expected_number_of_doublets / len(solo_scores) > .5:
            print('''Make sure you actually expect more than half your cells
                   to be doublets. If not change your
                   -e parameter value''')
        assert k > 0
        idx = np.argpartition(solo_scores, k)
        threshold = np.max(solo_scores[idx[:k]])
        is_solo_doublet = solo_scores > threshold
    else:
        is_solo_doublet = solo_scores > .5

    np.save(os.path.join(args.out_dir, 'is_doublet.npy'), is_solo_doublet[:num_cells])
    np.savetxt(os.path.join(args.out_dir, 'is_doublet.csv'), is_solo_doublet[:num_cells], delimiter=",")

    np.save(os.path.join(args.out_dir, 'is_doublet_sim.npy'), is_solo_doublet[num_cells:])

    np.save(os.path.join(args.out_dir, 'preds.npy'), order_pred[:num_cells])
    np.savetxt(os.path.join(args.out_dir, 'preds.csv'), order_pred[:num_cells], delimiter=",")

    smoothed_preds = knn_smooth_pred_class(X=latent, pred_class=is_doublet[:num_cells])
    np.save(os.path.join(args.out_dir, 'smoothed_preds.npy'), smoothed_preds)

    if args.anndata_output and data_ext == '.h5ad':
        scvi_data.obs['is_doublet'] = is_solo_doublet[:num_cells]
        scvi_data.obs['logit_scores'] = logit_doublet_score[:num_cells]
        scvi_data.obs['softmax_scores'] = doublet_score[:num_cells]
        scvi_data.write(os.path.join(args.out_dir, "soloed.h5ad"))

    if args.plot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import seaborn as sns

        train_solo_scores = solo_scores[solo.train_indices]
        validation_solo_scores = solo_scores[solo.validation_indices]
        test_solo_scores = solo_scores[solo.test_indices]

        train_fpr, train_tpr, _ = roc_curve(training_is_doublet_known, train_solo_scores)
        val_fpr, val_tpr, _ = roc_curve(validation_is_doublet_known, validation_solo_scores)
        test_fpr, test_tpr, _ = roc_curve(test_is_doublet_known, test_solo_scores)

        # plot ROC
        plt.figure()
        plt.plot(train_fpr, train_tpr, label='Train')
        plt.plot(val_fpr, val_tpr, label='Test')
        plt.plot(test_fpr, test_tpr, label='Test')
        plt.gca().set_xlabel('False positive rate')
        plt.gca().set_ylabel('True positive rate')
        plt.legend()
        plt.savefig(os.path.join(args.out_dir, 'roc.pdf'))
        plt.close()

        train_precision, train_recall, _ = precision_recall_curve(training_is_doublet_known, train_solo_scores)
        val_precision, val_recall, _ = precision_recall_curve(validation_is_doublet_known, validation_solo_scores)
        test_precision, test_recall, _ = precision_recall_curve(test_is_doublet_known, test_solo_scores)
        # plot accuracy
        plt.figure()
        plt.plot(train_precision, train_recall, label='Train')
        plt.plot(val_precision, val_recall, label='Validation')
        plt.plot(test_precision, test_recall, label='Test')
        plt.gca().set_xlabel('Recall')
        plt.gca().set_ylabel('Precision')
        plt.legend()
        plt.savefig(os.path.join(args.out_dir, 'precision_recall.pdf'))
        plt.close()

        # plot distributions
        obs_indices = solo_scores.test_indices[solo_scores.test_indices < num_cells]
        sim_indices = solo_scores.test_indices[solo_scores.test_indices > num_cells]

        plt.figure()
        sns.distplot(solo_scores[sim_indices], label='Simulated')
        sns.distplot(solo_scores[obs_indices], label='Observed')
        plt.legend()
        plt.savefig(os.path.join(args.out_dir, 'sim_vs_obs_dist.pdf'))
        plt.close()

        plt.figure()
        sns.distplot(solo_scores[:num_cells], label='Observed')
        plt.legend()
        plt.savefig(os.path.join(args.out_dir, 'real_cells_dist.pdf'))
        plt.close()

        scvi_umap = umap.UMAP(n_neighbors=16).fit_transform(latent)
        fig, ax = plt.subplots(1, 1, figsize=(10, 10))
        ax.scatter(scvi_umap[:, 0], scvi_umap[:, 1],
                   c=doublet_score[:num_cells], s=8, cmap="GnBu")

        ax.set_xlabel("UMAP 1")
        ax.set_ylabel("UMAP 2")
        ax.set_xticks([], [])
        ax.set_yticks([], [])
        fig.savefig(os.path.join(args.out_dir, 'umap_solo_scores.pdf'))

###############################################################################
# __main__
###############################################################################


if __name__ == '__main__':
    main()
