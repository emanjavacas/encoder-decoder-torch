
import os
import copy
import torch.optim as optim

from seqmod.modules.encoder_decoder import make_rnn_encoder_decoder
from seqmod.misc import Trainer, Checkpoint, PairedDataset
from seqmod.misc.loggers import StdLogger
from seqmod import utils as u

from train_skipthought import make_validation_hook, make_report_hook


def make_lr_hook(optimizer, factor, checkpoints):
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=checkpoints, gamma=factor)

    def hook(trainer, epoch, batch_num, checkpoint):
        loss = trainer.validate_model()
        trainer.log("validation_end", {"epoch": epoch, "loss": loss.pack()})
        lr = next(iter(trainer.optimizer.param_groups))['lr']
        scheduler.step(loss.reduce())
        newlr = next(iter(trainer.optimizer.param_groups))['lr']
        trainer.log("info", "LR update {:g} => {:g}".format(lr, newlr))

    return hook


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    # data
    parser.add_argument('--path', nargs='+', required=True)
    parser.add_argument('--dict_path', required=True)
    parser.add_argument('--dev_path')
    parser.add_argument('--max_size', type=int, default=100000)
    parser.add_argument('--max_len', type=int, default=100)
    parser.add_argument('--lower', action='store_true')
    parser.add_argument('--num', action='store_true')
    parser.add_argument('--level', default='word')
    parser.add_argument('--dev_split', type=float, default=0.05)
    # model
    parser.add_argument('--emb_dim', type=int, default=620)
    parser.add_argument('--hid_dim', type=int, default=2400)
    parser.add_argument('--num_layers', type=int, default=1)
    parser.add_argument('--cell', default='GRU')
    parser.add_argument('--encoder_summary', default='last')
    parser.add_argument('--sampled_softmax', action='store_true')
    parser.add_argument('--tie_weights', action='store_true')
    parser.add_argument('--train_init', action='store_true')
    parser.add_argument('--init_embeddings', action='store_true')
    parser.add_argument('--embeddings_path',
                        default='/home/corpora/word_embeddings/' +
                        'glove.840B.300d.txt')
    parser.add_argument('--reverse', action='store_true')
    # training
    parser.add_argument('--dropout', type=float, default=0.15)
    parser.add_argument('--word_dropout', type=float, default=0.0)
    parser.add_argument('--optimizer', default='Adam')
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--lr_schedule_checkpoints', type=int, default=1)
    parser.add_argument('--lr_schedule_factor', type=float, default=1)
    parser.add_argument('--max_norm', type=float, default=5.)
    parser.add_argument('--patience', default=0, type=int)
    parser.add_argument('--epochs', type=int, default=5)
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--device', default='cpu')
    parser.add_argument('--checkpoint', type=int, default=1000)
    parser.add_argument('--num_checkpoints', type=int, default=20)
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()

    print("Loading data...")
    d = u.load_model(args.dict_path)
    src_d, trg_d = d, d
    if args.reverse:
        trg_d = copy.deepcopy(d)
        trg_d.align_right = True
    dicts = {'src': src_d, 'trg': trg_d}

    print("Building model...")
    m = make_rnn_encoder_decoder(
        args.num_layers, args.emb_dim, args.hid_dim, d, cell=args.cell, bidi=True,
        encoder_summary=args.encoder_summary, dropout=args.dropout,
        sampled_softmax=args.sampled_softmax, reuse_hidden=False,
        add_init_jitter=False, input_feed=False, att_type=None,
        context_feed=True, reverse=args.reverse, train_init=args.train_init,
        tie_weights=args.tie_weights, word_dropout=args.word_dropout)

    print(m)
    print('* number of params: ', sum(p.nelement() for p in m.parameters()))

    u.initialize_model(
        m, rnn={'type': 'rnn_orthogonal', 'args': {'forget_bias': True}},
        emb={'type': 'uniform', 'args': {'a': -0.1, 'b': 0.1}})

    if args.init_embeddings:
        m.encoder.embeddings.init_embeddings_from_file(
            args.embeddings_path, verbose=True)

    m.to(device=args.device)

    optimizer = getattr(optim, args.optimizer)(m.parameters(), lr=args.lr)
    # validation hook
    checkpoint, logfile = None, None
    if not args.test:
        checkpoint = Checkpoint('Skipthought', keep=6, mode='nlast')
        checkpoint.setup(args)
        logfile = checkpoint.checkpoint_path('training.log')
    # reporting
    logger = StdLogger(outputfile=logfile)
    report_hook = make_report_hook()
    # no early stopping
    validation_hook = make_validation_hook(0, checkpoint)
    if args.patience:
        print("Ignoring early stopping parameters")
    # lr_hook
    lr_hook = None
    if args.lr_schedule_factor < 1.0:
        lr_hook = make_lr_hook(
            optimizer, args.lr_schedule_factor, args.lr_schedule_checkpoints)

    valid = None
    if args.dev_path and os.path.isfile(args.dev_path):
        valid = u.load_model(args.dev_path)
        valid = PairedDataset(
            valid['p1'], valid['p2'], dicts,
            batch_size=args.batch_size, fitted=True, device=args.device
        ).sort_(sort_by='trg')

    for epoch in range(args.epochs):
        for idx, path in enumerate(args.path):
            # prepare data subset
            print("Training on subset [{}/{}]: {}".format(idx+1, len(args.path), path))
            print("Loading data...")
            train = u.load_model(path)
            print("Preparing dataset")
            train = PairedDataset(
                train['p1'], train['p2'], dicts,
                batch_size=args.batch_size, fitted=True, device=args.device)
            if valid is None:
                print("Loading validation set")
                train, valid = train.splits(dev=None, test=args.dev_split, shuffle=True)
                valid.sort_(sort_by='trg')
            train.sort_(sort_by='trg')

            # setup trainer
            print("Starting training")
            trainer = Trainer(m, {'train': train, 'valid': valid}, optimizer,
                              losses=('ppl',), max_norm=args.max_norm)
            trainer.add_loggers(logger)
            trainer.add_hook(validation_hook, num_checkpoints=args.num_checkpoints)
            trainer.add_hook(report_hook, num_checkpoints=args.num_checkpoints)
            if args.lr_schedule_factor < 1.0:
                trainer.add_hook(lr_hook, num_checkpoints=args.num_checkpoints)
            # train
            trainer.train(1, args.checkpoint, shuffle=True)
            # ensure model is on gpu after training
            m.to(device=args.device)

            del train, trainer

    if not args.test:
        if not u.prompt("Do you want to keep intermediate results? (yes/no)"):
            checkpoint.remove()
