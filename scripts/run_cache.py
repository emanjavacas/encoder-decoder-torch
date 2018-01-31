
import os

import tqdm
import torch
import torch.nn.functional as F
from torch.autograd import Variable

from seqmod.misc import text_processor, BlockDataset, LossStatistics
from seqmod.loaders import load_lines
from seqmod.modules.cache import Cache
import seqmod.utils as u

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--path', required=True)
    parser.add_argument('--model_path', type=str)
    parser.add_argument('--lower', action='store_true')
    parser.add_argument('--num', action='store_true')
    parser.add_argument('--level', default='char')
    parser.add_argument('--alpha', default=0.1, type=float)
    parser.add_argument('--theta', default=0.1, type=float)
    parser.add_argument('--mode', default='linear')
    # test
    parser.add_argument('--batch_size', default=50, type=int)
    parser.add_argument('--bptt', default=35, type=int)
    parser.add_argument('--gpu', action='store_true')
    args = parser.parse_args()

    print("Loading model...")
    model = u.load_model(os.path.join(args.model_path, 'model.pt'))
    d = model.embeddings.d
    if args.gpu:
        model.cuda()
    else:
        model.cpu()
    model.eval()
    model.hidden_state = {}

    print("Loading data...")
    processor = text_processor(lower=args.lower, num=args.num, level=args.level)
    if os.path.isfile(os.path.join(args.path, 'test.txt')):
        path = os.path.join(args.path, 'test.txt')
        test = BlockDataset(load_lines(path, processor=processor), d,
                            args.batch_size, args.bptt, gpu=args.gpu,
                            evaluation=True)
    else:
        _, test = BlockDataset(
            load_lines(args.path, processor=processor), d,
            args.batch_size, args.bptt, gpu=args.gpu
        ).splits(test=args.test_split, dev=None)

    alpha, theta = args.alpha, args.theta
    cache = Cache(model.hid_dim, 500, len(d), gpu=args.gpu)
    loss, hidden = LossStatistics('ppl'), None

    def batch_index_add_(t, index, src):
        """
        t: (batch x vocab)
        index: (batch x cache_size)
        src: (batch x cache_size)
        """
        (batch, vocab) = t.size()
        ex = torch.arange(0, batch, out=t.new()).unsqueeze(1).long() * vocab
        t.view(-1).index_add_(
            0,
            (index + ex).view(-1),
            src.view(-1))
        
    
    for (source, targets) in tqdm.tqdm(test):
        # outs: (bptt x batch x hid)
        outs, hidden, _ = model(source, hidden=hidden)

        for out, target in zip(outs, targets):
            # (batch x vocab)
            logits = model.project(out, normalize=False)

            if cache.stored > 0:  # only interpolate after first step
                # (batch x cache_size)
                cache_logits, vals = u.wrap_variables(
                    cache.query(out.data), volatile=True)

                # interpolate
                if args.mode == 'linear':
                    cache_prob = alpha * F.softmax(theta * cache_logits, dim=1)
                    prob = (1 - alpha) * F.softmax(logits, dim=1)
                    batch_index_add_(prob.data, vals.data, cache_prob.data)
                elif args.mode == 'global':
                    cache_logits = theta * cache_logits + alpha
                    batch_index_add_(logits.data, vals.data, cache_logits.data)
                    prob = F.softmax(logits, dim=1)

            else:
                prob = F.softmax(logits, dim=1)

            loss.add(u.unwrap_variables(F.nll_loss(prob.log(), target)),
                     target.nelement())
            cache.add(out.data.unsqueeze(0), target.data.unsqueeze(0))

    print(loss.reduce())