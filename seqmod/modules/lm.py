
import logging

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.utils.rnn import pad_packed_sequence as unpack

from seqmod.modules.torch_utils import init_hidden_for, repackage_hidden
from seqmod.modules.torch_utils import swap, select_cols
from seqmod.modules.embedding import Embedding
from seqmod.modules import rnn
from seqmod.modules.ff import MaxOut
from seqmod.modules.softmax import FullSoftmax, MixtureSoftmax, SampledSoftmax
from seqmod.modules.attention import Attention
from seqmod.misc.beam_search import Beam
from seqmod.modules.exposure import scheduled_sampling
from seqmod.modules.torch_utils import pack_sort, get_last_token


def strip_post_eos(sents, eos):
    """
    remove suffix of symbols after <eos> in generated sentences
    """
    out = []
    for sent in sents:
        for idx, item in enumerate(sent):
            if item == eos:
                out.append(sent[:idx+1])
                break
        else:
            out.append(sent)
    return out


def read_batch(m, seed_texts, gpu=False, **kwargs):
    """
    Computes the hidden states for a bunch of seeds in iterative fashion.
    This is currently being done so because LM doesn't use padding and
    input seeds might have different sizes (in the future this will have
    to be implemented using pack_padded_sequence which allows for variable
    length inputs)

    Parameters:
    -----------
    seed_texts: list of lists of ints

    Returns:
    --------
    prev: torch.LongTensor (1 x batch_size), sampled symbols in the batch
    hidden: torch.FloatTensor (num_layers x batch_size x hid_dim)
    """
    prev, hs, cs = [], [], []

    for seed_text in seed_texts:
        # split last (which will be returned as first input for the generation)
        seed_text, prev_i = seed_text[:-1], seed_text[-1]
        # accumulate first input to generation
        prev.append(prev_i)
        # run the RNN
        inp = Variable(torch.LongTensor(seed_text).unsqueeze(1), volatile=True)
        _, hidden, _ = m(inp.cuda() if gpu else inp, **kwargs)
        # accumulate hidden states
        if m.cell.startswith('LSTM'):
            h, c = hidden
            hs.append(h), cs.append(c)
        else:
            hs.append(hidden)
    # pack output
    prev = torch.LongTensor(prev).unsqueeze(0)
    if m.cell.startswith('LSTM'):
        return prev, (torch.cat(hs, 1), torch.cat(cs, 1))
    else:
        return prev, torch.cat(hs, 1)


class Generator(object):
    """
    General generator class for language models.

    Parameters:
    -----------

    model: LM, fitted LM model to use for generation.
    d: Dict, dictionary fitted on the LM's input vocabulary.
    gpu: bool, whether to run generation on the gpu.
    """
    def __init__(self, model, d, gpu=False):
        self.gpu = gpu
        self.model = model
        self.d = d
        self.bos, self.eos = self.d.get_bos(), self.d.get_eos()

    def _seed(self, seed_texts, batch_size, bos, eos, **kwargs):
        """
        Handles the input to the actual generation method taking into account
        multiple variables. If seed_texts are given, it takes care of reading
        them to initialize the hidden state. In that case the input to the
        generation is a symbol predicted right after reading the batch.
        In case of generation from scratch (seed_texts is None), it will
        feed in the <bos> symbol if it exists, otherwise, it will just randomly
        sample a symbol from the vocabulary.

        If seed_texts isn't given or its length is just one, the generation
        will be done in a batch of size `batch_size` thus generating as many
        outputs.

        Parameters:
        -----------

        seed_texts: list (of list (of string)) or None,
            Seed text to initialize the generator. It should be an iterable of
            lists of strings over vocabulary tokens. If seed_texts is given and
            is of length 1, the seed will be broadcasted to batch_size.
        batch_size: int, number of items in the seed batch.
        bos: bool, whether to prefix the seed with the bos_token. Only used if
            seed_texts is given and the dictionary has a bos_token.
        eos: bool, whether to suffix the seed with the eos_token. Only used if
            seed_texts is given and the dictionary has a eos_token.
        kwargs: extra LM.forward parameters

        Returns:
        --------

        prev: (1 x batch_size), first integer token to feed into the generator
        hidden: hidden state to seed the generator, may be None if no seed text
            was passed to the generation function.
        """
        hidden = None

        if seed_texts is not None:  # read input seed batch
            seed_texts = [[self.d.index(i) for i in s] for s in seed_texts]

            # append <eos> and <bos> if necessary
            if bos and self.bos is not None:  # prepend bos to seeds
                seed_texts = [[self.bos] + s for s in seed_texts]
            if eos and self.eos is not None:  # append eos to seeds
                seed_texts = [s + [self.eos] for s in seed_texts]

            # read batch
            prev, hidden = read_batch(
                self.model, seed_texts, gpu=self.gpu, **kwargs)

            # extend to batch size if only single seed
            if len(seed_texts) == 1:
                prev = prev.repeat(1, batch_size)
                if self.model.cell.startswith('LSTM'):
                    hidden = (hidden[0].repeat(1, batch_size, 1),
                              hidden[1].repeat(1, batch_size, 1))
                else:
                    hidden = hidden.repeat(1, batch_size, 1)

        elif self.eos is not None:  # initialize with <eos>
            prev = torch.LongTensor([self.eos] * batch_size).unsqueeze(0)

        else:  # random uniform sample from vocab
            prev = torch.LongTensor(1, batch_size).random_(self.model.vocab)

        # wrap prev in variable
        prev = Variable(prev.cuda() if self.gpu else prev, volatile=True)

        return prev, hidden

    def argmax(self, seed_texts=None, max_seq_len=25, batch_size=1,
               ignore_eos=False, bos=False, eos=False, **kwargs):
        """
        Generate a sequence sampling the element with highest probability
        in the output distribution at each generation step.
        """
        prev, hidden = self._seed(seed_texts, batch_size, bos, eos)
        hyps, scores = [], 0
        mask = torch.ones(batch_size).long()

        for _ in range(max_seq_len):
            outs, hidden, _ = self.model(prev, hidden=hidden, **kwargs)
            outs = self.model.project(outs)
            score, prev = outs.max(1)
            score, prev = score.squeeze(), prev.t()
            hyps.append(prev.squeeze().data.tolist())

            if self.eos is not None and not ignore_eos:
                mask = mask * (prev.squeeze().data.cpu() != self.eos).long()
                if mask.sum() == 0:
                    break
                # 0-mask scores for finished batch examples
                score.data[mask == 0] = 0

            scores += score.data

        return scores.tolist(), list(zip(*hyps))

    def beam(self, width=5, seed_texts=None, max_seq_len=25,
             ignore_eos=False, bos=False, eos=False, **kwargs):
        """
        Approximation to the highest probability output over the generated
        sequence using beam search.
        """
        prev, hidden = self._seed(seed_texts, 1, bos, eos)
        eos = self.eos if not ignore_eos else None
        beam = Beam(width, prev.squeeze().data[0], eos=eos)

        while beam.active and len(beam) < max_seq_len:
            prev = Variable(beam.get_current_state().unsqueeze(0),
                            volatile=True)
            outs, hidden, _ = self.model(prev, hidden=hidden, **kwargs)
            outs = self.model.project(outs)
            beam.advance(outs.data)

            if self.model.cell.startswith('LSTM'):
                hidden = (swap(hidden[0], 1, beam.get_source_beam()),
                          swap(hidden[1], 1, beam.get_source_beam()))
            else:
                hidden = swap(hidden, 1, beam.get_source_beam())

        scores, hyps = beam.decode(n=width)

        return scores, hyps

    def sample(self, temperature=1., seed_texts=None, max_seq_len=25,
               batch_size=1, ignore_eos=False, bos=False, eos=False,
               **kwargs):
        """
        Generate a sequence multinomially sampling from the output
        distribution at each generation step. The output distribution
        can be tweaked by the input parameter `temperature`.
        """
        prev, hidden = self._seed(
            seed_texts, batch_size, bos, eos, temperature=temperature)
        batch_size = prev.size(1)  # not equal to input if seed_texts
        hyps, scores = [], 0
        mask = torch.zeros(batch_size).long() + 1

        for _ in range(max_seq_len):
            outs, hidden, _ = self.model(prev, hidden=hidden, **kwargs)
            outs = self.model.project(outs)
            prev = outs.div_(temperature).exp().multinomial(1).t()
            score = select_cols(outs.data.cpu(), prev.squeeze().data.cpu())
            hyps.append(prev.squeeze().data.tolist())

            if self.eos is not None and not ignore_eos:
                mask = mask * (prev.squeeze().data.cpu() != self.eos).long()
                if mask.sum() == 0:
                    break
                score[mask == 0] = 0  # 0-mask scores for finished examples

            scores += score

        return scores.tolist(), list(zip(*hyps))


class AttentionalProjection(nn.Module):
    def __init__(self, att_dim, hid_dim, emb_dim):
        super(AttentionalProjection, self).__init__()
        self.attn = Attention(
            hid_dim, att_dim, hid_dim2=emb_dim, scorer='bahdanau')
        self.hid2hid = nn.Linear(hid_dim, hid_dim)
        self.emb2hid = nn.Linear(emb_dim, hid_dim)

    def forward(self, outs, emb):
        """
        Runs attention for a given input sequence

        Returns: output, weights
        --------
        output: torch.Tensor (seq_len x batch_size x hid_dim)
        weights: list of torch.Tensor(batch_size x 0:t-1) of length seq_len
        """
        emb_att = self.attn.scorer.project_enc_outs(emb)
        output, weights = [], []
        for idx, hid in enumerate(outs):
            t = max(0, idx-1)  # use same hid at t=0
            context, weight = self.attn(
                outs[t], emb[:max(1, t)], enc_att=emb_att[:max(1, t)])
            output.append(self.hid2hid(hid) + self.emb2hid(context))
            weights.append(weight)
        return torch.stack(output), weights


class BaseLM(nn.Module):
    """
    Abstract LM Class
    """
    def __init__(self, *args, exposure_rate=1.0, **kwargs):
        # LM training data
        self.hidden_state = {}
        self.exposure_rate = exposure_rate

        super(BaseLM, self).__init__()

    def is_cuda(self):
        return next(self.parameters()).is_cuda

    def n_params(self, trainable=True):
        cnt = 0
        for p in self.parameters():
            if trainable and not p.requires_grad:
                continue
            cnt += p.nelement()

        return cnt

    def freeze_submodule(self, module, flag=False):
        for p in getattr(self, module).parameters():
            p.requires_grad = flag

    def set_dropout(self, dropout):
        for m in self.children():
            if hasattr(m, 'dropout'):
                m.dropout = dropout

    def forward(self, inp, **kwargs):
        raise NotImplementedError

    def loss(self, batch, test=False):
        raise NotImplementedError

    def generate(self, d, seed_texts=None, max_seq_len=25,
                 gpu=False, method='sample', temperature=1., width=5,
                 bos=False, eos=False, ignore_eos=False, batch_size=10,
                 **kwargs):
        """
        Generate text using a specified method (argmax, sample, beam)

        Parameters:
        -----------
        d: Dict used during training to fit the vocabulary
        seed_texts: None or list of sentences to use as seed for the generator
        max_seq_len: int, maximum number of symbols to be generated. The output
            might actually be less than this number if the Dict was fitted with
            a <eos> token (in which case generation will end after the first
            generated <eos>)
        gpu: bool, whether to generate on the gpu
        method: str, one of 'sample', 'argmax', 'beam' (check the corresponding
            functions in Generator for more info)
        temperature: float, temperature for multinomial sampling (only applies
            to method 'sample')
        width: int, beam size width (only applies to the 'beam' method)
        bos: bool, whether to prefix the seed with the bos_token. Only used if
            seed_texts is given and the dictionary has a bos_token.
        eos: bool, whether to suffix the seed with the eos_token. Only used if
            seed_texts is given and the dictionary has a eos_token.
        ignore_eos: bool, whether to stop generation after hitting <eos> or not
        batch_size: int, number of parallel generations (only used if
            seed_texts is None)

        Returns:
        --------
        scores: list of floats, unnormalized scores, one for each hypothesis
        hyps: list of lists, decoded hypotheses in integer form
        """
        if self.training:
            logging.warn("Generating in training modus!")

        generator = getattr(Generator(self, d, gpu=gpu), method)
        scores, hyps = generator(
            seed_texts=seed_texts, max_seq_len=max_seq_len,
            batch_size=batch_size, ignore_eos=ignore_eos, bos=bos, eos=eos,
            # sample-only
            temperature=temperature,
            # beam-only
            width=width, **kwargs)

        if not ignore_eos and d.get_eos() is not None:
            # strip content after <eos> for each batch
            hyps = strip_post_eos(hyps, d.get_eos())

        norm_scores = [s/len(hyps[idx]) for idx, s in enumerate(scores)]

        return norm_scores, hyps

    def predict_proba(self, inp, **kwargs):
        """
        Compute the probability assigned by the model to an input sequence.
        In the future this should use pack_padded_sequence to run prediction
        on multiple input sentences at once.

        Parameters:
        -----------
        inp: Variable(seq_len x batch_size)
        kwargs: other model parameters

        Returns:
        --------
        np.array (batch) of representing the log probability of the input
        """
        if self.training:
            logging.warn("Generating in training modus!")

        if self.is_cuda():
            inp = inp.cuda()

        # compute output
        outs, *_ = self(inp, **kwargs)
        outs = self.project(outs, reshape=True)  # (seq_len x batch x vocab)

        # select
        outs, index = outs[:-1].data.cpu().numpy(), inp[1:].data.cpu().numpy()
        seq_len, batch = np.ogrid[0:index.shape[0], 0:index.shape[1]]
        # (batch x seq_len)
        log_probs = outs.transpose(2, 0, 1)[index, seq_len, batch].T

        # normalize by length
        log_probs = log_probs.sum(1) / log_probs.shape[1]

        return np.exp(log_probs)


class LM(BaseLM):
    """
    Vanilla RNN-based language model.

    Parameters:
    -----------

    - emb_dim: int, embedding size.
    - hid_dim: int, hidden dimension of the RNN.
    - d: Dict.
    - num_layers: int, number of layers of the RNN.
    - cell: str, one of GRU, LSTM, RNN.
    - bias: bool, whether to include bias in the RNN.
    - dropout: float, amount of dropout to apply in between layers.
    - word_dropout: float.
    - att_dim: int, whether to add an attention module of dimension `att_dim`
        over the prefix. No attention will be added if att_dim is None or 0.
    - tie_weights: bool, whether to tie input and output embedding layers.
        In case of unequal emb_dim and hid_dim values a linear project layer
        will be inserted after the RNN to match back to the embedding dim
    - mixtures: int, use a mixture of softmaxes in the output (only if the value
        is strictly positive).
    - deepout_layers: int, whether to add deep output after hidden layer and
        before output projection layer. No deep output will be added if
        deepout_layers is 0 or None.
    - deepout_act: str, activation function for the deepout module in camelcase
    - maxouts: int, only used if deepout_act is MaxOut (number of parts to use
        to compose the non-linearity function).
    """
    def __init__(self, emb_dim, hid_dim, d, num_layers=1,
                 cell='GRU', bias=True, dropout=0.0, word_dropout=0.0,
                 att_dim=0, tie_weights=False, mixtures=0,
                 train_init=False, add_init_jitter=False, sampled_softmax=False,
                 deepout_layers=0, deepout_act='MaxOut', maxouts=2,
                 exposure_rate=1.0):

        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.num_layers = num_layers
        self.tie_weights = tie_weights
        self.cell = cell
        self.bias = bias
        self.train_init = train_init
        self.add_init_jitter = add_init_jitter
        self.dropout = dropout
        super(LM, self).__init__()

        self.exposure_rate = exposure_rate

        # Embeddings
        self.embeddings = Embedding.from_dict(d, emb_dim, p=word_dropout)

        # RNN
        if cell.startswith('RHN'):
            self.num_layers = 1  # RHN layers don't add to output dims

        # train init
        self.h_0 = None
        if self.train_init:
            init_size = self.num_layers, 1, self.hid_dim
            self.h_0 = nn.Parameter(torch.Tensor(*init_size).zero_())

        try:
            cell = getattr(nn, cell)
        except AttributeError:  # assume custom rnn cell
            cell = getattr(rnn, cell)

        self.rnn = cell(
            self.emb_dim, self.hid_dim,
            num_layers=num_layers, bias=bias, dropout=dropout)

        # (optional) attention
        if att_dim > 0:
            if self.cell not in ('RNN', 'GRU'):
                raise ValueError("Currently only RNN, GRU supports attention")
            self.attn = AttentionalProjection(att_dim, hid_dim, emb_dim)
        self.has_attention = hasattr(self, 'attn')

        # Output projection
        if mixtures > 0:
            self.project = MixtureSoftmax(
                hid_dim, emb_dim, len(self.embeddings.d),
                tie_weights=tie_weights, dropout=dropout, mixtures=mixtures)
        elif sampled_softmax:
            self.project = SampledSoftmax(
                hid_dim, emb_dim, len(self.embeddings.d), nsampled=8192,
                tie_weights=tie_weights, dropout=dropout,
                deepout_layers=deepout_layers, deepout_act=MaxOut, maxouts=maxouts)
        else:
            self.project = FullSoftmax(
                hid_dim, emb_dim, len(self.embeddings.d),
                tie_weights=tie_weights, dropout=dropout,
                deepout_layers=deepout_layers, deepout_act=MaxOut, maxouts=maxouts)

        if tie_weights:
            self.project.tie_embedding_weights(self.embeddings)

    def init_hidden_for(self, inp):
        return init_hidden_for(
            inp, 1, self.num_layers, self.hid_dim, self.cell,
            h_0=self.h_0, add_init_jitter=self.add_init_jitter,
            training=self.training)

    def forward(self, inp, hidden=None, **kwargs):
        """
        Parameters:
        -----------
        inp: torch.Tensor (seq_len x batch_size)
        hidden: None or torch.Tensor (num_layers x batch_size x hid_dim)

        Returns:
        --------
        outs: torch.Tensor (seq_len * batch_size x vocab)
        hidden: see output of RNN, GRU, LSTM in torch.nn
        weights: None or list of weights (batch_size x seq_len),
            It will only be not None if attention is provided.
        """
        # Embeddings
        emb = self.embeddings(inp)

        if not self.cell.startswith('RHN'):
            emb = F.dropout(emb, p=self.dropout, training=self.training)

        # RNN
        hidden = hidden if hidden is not None else self.init_hidden_for(emb)
        outs, hidden = self.rnn(emb, hidden)

        # (dropout after RNN)
        outs = F.dropout(outs, p=self.dropout, training=self.training)

        # (optional attention)
        weights = None
        if self.has_attention:
            outs, weights = self.attn(outs, emb)

        return outs, hidden, weights

    def loss(self, batch_data, test=False, use_schedule=False):
        # unpack data
        (src, trg) = batch_data

        # eventually get data from previous batch
        hidden = self.hidden_state.get('hidden')

        # run RNN
        if use_schedule and self.exposure_rate < 1.0:
            outs = []
            for step, t in enumerate(src):
                if use_schedule and step > 0:
                    t = scheduled_sampling(
                        t, outs[-1], self.project, self.exposure_rate)
                    t = Variable(t, volatile=not self.training)
                out, hidden, _ = self(t.unsqueeze(0), hidden=hidden)
                outs.append(out.squeeze(0))
            outs = torch.stack(outs, 0)

        else:
            outs, hidden, _ = self(src, hidden=hidden)

        # store hidden for next batch
        self.hidden_state['hidden'] = repackage_hidden(hidden)

        # compute loss and backward
        if isinstance(self.project, SampledSoftmax) and self.training:
            logits, new_trg = self.project(
                outs, targets=trg.view(-1), normalize=False, reshape=False)
            loss = F.cross_entropy(logits, new_trg, size_average=True)
        else:
            loss = F.nll_loss(self.project(outs), trg.view(-1), size_average=True)

        if not test:
            loss.backward()

        return (loss.data[0], ), src.nelement()


class ConditionalLM(LM):
    """
    Variant of LM that is abl to deal with padding. Currently, this would slowdown
    training proportionally to the amount of padding in the training set due to
    the redundant softmax (RNNs, however, are able to avoid doing computationg over
    padding tokens thanks to PackedSequence).
    The advantage is that conditioning becomes much easier since we can fill batches
    with full sentences.

    - conds: list of dicts for the conditions
        Note that the conditions should be specified in the same order as
        they are passed into the model at run-time.
    """
    def __init__(self, *args, cond_dicts=None, cond_dims=None, **kwargs):
        super(ConditionalLM, self).__init__(*args, **kwargs)
        self.conditional = cond_dicts is not None

        if cond_dicts is not None:
            rnn_inp_size = self.rnn.input_size
            if isinstance(cond_dims, int):
                cond_dims = (cond_dims,) * len(cond_dicts)

            cond_embs = []
            for cond_dict, cond_dim in zip(cond_dicts, cond_dims):
                cond_embs.append(Embedding.from_dict(cond_dict, cond_dim))
                rnn_inp_size += cond_dim
            self.cond_embs = nn.ModuleList(cond_embs)

            # reset rnn to new input size
            self.rnn = type(self.rnn)(rnn_inp_size, self.hid_dim,
                                      num_layers=self.num_layers, bias=self.bias,
                                      dropout=self.dropout)

    def forward(self, inp, lengths=None, hidden=None, conds=None, **kwargs):
        """
        Parameters:
        -----------
        inp: torch.Tensor (seq_len x batch_size)
        hidden: None or torch.Tensor (num_layers x batch_size x hid_dim)
        lengths: torch.LongTensor (batch_size)
        conds: None or tuple of torch.Tensor (seq_len x batch_size) of length
            equal to the number of model conditions. `conditions` are required
            in case of a CLM.

        Returns:
        --------
        outs: torch.Tensor (seq_len * batch_size x vocab)
        hidden: see output of RNN, GRU, LSTM in torch.nn
        weights: None or list of weights (batch_size x seq_len),
            It will only be not None if attention is provided.
        """
        # Embeddings
        emb = self.embeddings(inp)

        # Conditions
        if hasattr(self, 'conds') and self.conds is not None:
            if conds is None:
                raise ValueError("Conditional model expects `conds` as input")
            conds_out = []
            for cond_emb, cond in zip(self.conds, conds):
                conds_out.append(cond_emb(cond).unsqueeze(0).repeat(len(inp), 1))
            emb = torch.cat([emb, *conds_out], dim=2)

        if not self.cell.startswith('RHN'):
            emb = F.dropout(emb, p=self.dropout, training=self.training)

        # RNN
        hidden = hidden if hidden is not None else self.init_hidden_for(emb)
        emb, unsort = pack_sort(emb, lengths)
        outs, hidden = self.rnn(emb, hidden)
        outs, _ = unpack(outs)
        outs = outs[:, unsort]
        if self.cell.startswith('LSTM'):
            hidden = hidden[0][:, unsort, :], hidden[1][:, unsort, :]
        else:
            hidden = hidden[:, unsort, :]

        # (dropout after RNN)
        outs = F.dropout(outs, p=self.dropout, training=self.training)

        # (optional attention)
        weights = None
        if self.has_attention:
            outs, weights = self.attn(outs, emb)

        return outs, hidden, weights

    def loss(self, batch_data, test=False, use_schedule=False):
        # unpack data
        (src, trg), conds = batch_data, None
        if self.conds is not None:
            (src, *conds), (trg, *_) = src, trg
        (src, src_len), (trg, _) = src, trg

        # eventually get data from previous batch
        hidden = self.hidden_state.get('hidden')

        # run RNN
        if use_schedule:
            # needs running RNN step-by-step avoiding hidden updates over padding
            raise NotImplementedError

        else:
            outs, hidden, _ = self(
                src, lengths=src_len, hidden=hidden, conds=conds)

        # store hidden for next batch
        self.hidden_state['hidden'] = repackage_hidden(hidden)

        # compute loss and backward
        if isinstance(self.project, SampledSoftmax) and self.training:
            logits, new_targets = self.project(
                outs, targets=trg.view(-1), normalize=False, reshape=False)
            loss = F.cross_entropy(logits, new_targets, size_average=True)
        else:
            loss = F.nll_loss(self.project(outs), trg.view(-1), size_average=True)

        if not test:
            loss.backward()

        return (loss.data[0], ), src.nelement()
