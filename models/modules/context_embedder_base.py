#!/usr/bin/env python
import torch
import logging
import sys
from transformers import BertModel, ElectraModel
from torchnlp.word_to_vector import GloVe
import torchsnooper
import torch.nn as nn
import torch.nn.functional as F
from models.modules.GCN import GraphConvolution

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO,
                    stream=sys.stdout)
logger = logging.getLogger(__name__)

import torchsnooper
class AttnGRU(nn.Module):
    def __init__(self,hidden_dim,num_layers,kqv_dim):
        super(AttnGRU, self).__init__()

        # self.embedding=nn.Embedding(num_words,hidden_dim)
        self.gru=nn.GRU(hidden_dim,hidden_dim,num_layers,batch_first=True)
        self.attn=Attn(hidden_dim,kqv_dim)
        self.linear=nn.Linear(hidden_dim,2)
    # @torchsnooper.snoop()  
    def forward(self,input):
        # out=self.embedding(input)
        out=self.attn(input)
        out,hidden=self.gru(out)
        out=self.linear(out[:,-1,:])
        return out


class MultiHead(nn.Module):
    def __init__(self,hidden_dim,kqv_dim,num_heads):
        super(MultiHead, self).__init__()


class Attn(nn.Module):
    def __init__(self,hidden_dim,kqv_dim):
        super(Attn, self).__init__()
        self.wk=nn.Linear(hidden_dim,kqv_dim)
        self.wq=nn.Linear(hidden_dim,kqv_dim)
        self.wv=nn.Linear(hidden_dim,kqv_dim)
        self.d=kqv_dim**0.5

    def forward(self, input):
        '''
        :param input: batch_size x seq_len x hidden_dim
        :return:
        '''
        k=self.wk(input)
        q=self.wq(input)
        v=self.wv(input)
        w=F.softmax(torch.bmm(q,k.transpose(-1,-2))/self.d,dim=-1)
        attn=torch.bmm(w,v)

        return attn

class ContextEmbedderBase(torch.nn.Module):
    def __init__(self):
        super(ContextEmbedderBase, self).__init__()

    def forward(self, *args, **kwargs) -> (torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor):
        """
        :param args:
        :param kwargs:
        :return: test_token_reps, support_token_reps, test_sent_reps, support_sent_reps
        """
        raise NotImplementedError()


class BertContextEmbedder(ContextEmbedderBase):
    def __init__(self, opt):
        super(BertContextEmbedder, self).__init__()
        self.opt = opt
        self.embedder = self.build_embedder()
        self.gcns = nn.ModuleList()
        # self.multihead_attn = nn.MultiheadAttention(embed_dim=opt.emb_dim, num_heads=3)
        for i in range(opt.gcn_layers):
            gcn = GraphConvolution(in_features=opt.emb_dim,
                                   out_features=200,
                                   edge_types=3,
                                   dropout=0.3 if i != (opt.gcn_layers - 1) else None,
                                   use_bn=True)
            self.gcns.append(gcn)

    def forward(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj:torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor = None,
            support_segment_ids: torch.Tensor = None,
            support_adj:torch.Tensor = None,
            support_nwp_index: torch.Tensor = None,
            support_input_mask: torch.Tensor = None,
    ) -> (torch.Tensor, torch.Tensor):
        """
        get context representation
        :param test_token_ids: (batch_size, test_len)
        :param test_segment_ids: (batch_size, test_len)
        :param test_nwp_index: (batch_size, test_len, 1)
        :param test_input_mask: (batch_size, test_len)

        ======= Support features ======
        We allow to only embed query to enable single sentence embedding, but such feature is NOT used now.
        (Separate embedding is achieved through special sub classes)

        :param support_token_ids: (batch_size, support_size, support_len)
        :param support_segment_ids: (batch_size, support_size, support_len)
        :param support_nwp_index: (batch_size, support_size, support_len, 1)
        :param support_input_mask: (batch_size, support_size, support_len)
        :return:
            if do concatenating representation:
                return (test_reps, support_reps, test_sent_reps, support_sent_reps):
                    test_reps, support_reps:  token reps (batch_size, support_size, nwp_sent_len, emb_len)
                    test_sent_reps, support_sent_reps: sent reps (batch_size, support_size, 1, emb_len)
            else do representation for a single sent (No support staff):
                return test_reps, shape is (batch_size, nwp_sent_len, emb_len)
        """
        if support_token_ids is not None:
            return self.concatenate_reps(
                test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask,
                support_token_ids, support_segment_ids, support_adj,support_nwp_index, support_input_mask,
            )
        else:
            return self.single_reps(test_token_ids, test_segment_ids,test_adj, test_nwp_index, test_input_mask,)

    def build_embedder(self):
        """ load bert here """
        return BertModel.from_pretrained(self.opt.bert_path)
    # @torchsnooper.snoop()                                                                                                                                                                                     
    def concatenate_reps(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj:torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor,
            support_segment_ids: torch.Tensor,
            support_adj:torch.Tensor,
            support_nwp_index: torch.Tensor,
            support_input_mask: torch.Tensor,
    ) -> (torch.Tensor, torch.Tensor):
        """ get token reps of a sent pair. """
        support_size = support_token_ids.shape[1]
        test_len = test_token_ids.shape[-1] - 2  # max len, exclude [CLS] and [SEP] token
        support_len = support_token_ids.shape[-1] - 1  # max len, exclude [SEP] token
        batch_size = support_token_ids.shape[0]
        ''' expand test input to shape: (batch_size, support_size, test_len)'''
        test_token_ids, test_segment_ids, test_input_mask, test_nwp_index = self.expand_test_item(
            test_token_ids, test_segment_ids, test_input_mask, test_nwp_index, support_size)
        ''' concat test and support TO support: (batch_size, support_size, test_len+ support_len)''' 
        input_ids = self.cat_test_and_support(test_token_ids, support_token_ids)
        segment_ids = self.cat_test_and_support(test_segment_ids, support_segment_ids)
        input_mask = self.cat_test_and_support(test_input_mask, support_input_mask)
        ''' flatten input   (batch_size * support_size, test_len+ support_len) '''
        input_ids, segment_ids, input_mask = self.flatten_input(input_ids, segment_ids, input_mask)
        test_nwp_index, support_nwp_index = self.flatten_index(test_nwp_index), self.flatten_index(support_nwp_index)
        ''' get concat reps  (batch_size * support_size, test_len+ support_len，emb)  '''
        sequence_output = self.embedder(input_ids, input_mask, segment_ids)[0]
        # sequence_output,_ = self.multihead_attn(sequence_output,sequence_output,sequence_output)
        ''' extract reps '''
        # select pure sent part, remove [SEP] and [CLS], notice: seq_len1 == seq_len2 == max_len.
        test_reps = sequence_output.narrow(-2, 1, test_len)  # shape:(batch_size * support_size, test_len, rep_size)
        support_reps = sequence_output.narrow(-2, 2 + test_len, support_len)  # shape:(batch_size * support_size, support_len, rep_size)
        test_reps_1 = test_reps
        support_reps_1 = support_reps
        if self.opt.gcns==1:
            logger.info("use GCNs !!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            support_adj = support_adj.contiguous().view(-1,3,support_len,support_len)
            # print("===================",support_reps.shape,support_adj.shape)
            for i in range(self.opt.gcn_layers):
                test_reps = self.gcns[i](test_reps, test_adj)
            for i in range(self.opt.gcn_layers):
                support_reps = self.gcns[i](support_reps, support_adj)
            # test_reps = 0.8 * test_reps + 0.2* test_reps_1
            # support_reps = 0.8* support_reps + 0.2 * support_reps_1
            # print(test_reps.shape,test_reps_1.shape)
            test_reps = torch.cat((test_reps,test_reps_1),2)
            support_reps = torch.cat((support_reps,support_reps_1),2)

        # select non-word-piece tokens' representation
        nwp_test_reps = self.extract_non_word_piece_reps(test_reps, test_nwp_index)
        nwp_support_reps = self.extract_non_word_piece_reps(support_reps, support_nwp_index)
        # resize to shape (batch_size, support_size, sent_len, emb_len)
        reps_size = nwp_test_reps.shape[-1]
        nwp_test_reps = nwp_test_reps.view(batch_size, support_size, -1, reps_size)
        nwp_support_reps = nwp_support_reps.view(batch_size, support_size, -1, reps_size)
        test_reps = test_reps.view(batch_size, support_size, -1, reps_size)
        support_reps = support_reps.view(batch_size, support_size, -1, reps_size)
        # Average token reps to get a whole sent reps
        # (batch_size, support_size, sent_len, emb_len)
        # test_sent_reps = self.get_sent_reps(test_reps, test_input_mask)
        # support_sent_reps = self.get_sent_reps(support_reps, support_input_mask)
        # out = AttnGRU(768,6,100)
        # print(out.shape)
        # return nwp_test_reps, nwp_support_reps, test_sent_reps, support_sent_reps
        return nwp_test_reps, nwp_support_reps, 1, 1

    def single_reps(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj: torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
    ) -> (torch.Tensor, torch.Tensor):
        """ get token reps of a single sent. """
        test_len = test_token_ids.shape[-1] - 2  # max len, exclude [CLS] and [SEP] token
        batch_size = test_token_ids.shape[0]
        ''' get bert reps '''
        test_sequence_output = self.embedder(test_token_ids, test_input_mask, test_segment_ids)[0]
        ''' extract reps '''
        # select pure sent part, remove [SEP] and [CLS], notice: seq_len1 == seq_len2 == max_len.
        test_reps = test_sequence_output.narrow(-2, 1, test_len)  # shape:(batch, test_len, rep_size)
        test_reps_1 = test_reps
        if self.opt.gcns==1:
            for i in range(self.opt.gcn_layers):
                test_reps = self.gcns[i](test_reps, test_adj)

            # test_reps = 0.8 * test_reps + 0.2* test_reps_1
            test_reps = torch.cat((test_reps,test_reps_1),2)
        # support_reps = torch.cat((support_reps,support_reps_1),1)
        # select non-word-piece tokens' representation
        nwp_test_reps = self.extract_non_word_piece_reps(test_reps, test_nwp_index)
        # get whole word reps, unsuqeeze to fit interface
        # test_sent_reps = self.get_sent_reps(test_reps.unsqueeze(1), test_input_mask.unsqueeze(1)).squeeze(1)
        return nwp_test_reps, 1

    def get_sent_reps(self, reps, input_mask):
        """
         Average token reps to get a whole sent reps
        :param reps:   (batch_size, support_size, sent_len, emb_len)
        :param input_mask:  (batch_size, support_size, sent_len)
        :return:  averaged reps (batch_size, support_size, sent_len, emb_len)
        """
        # print("========================",reps.shape)
        batch_size, support_size, sent_len, reps_size = reps.shape
        mask_len = input_mask.shape[-1]
        # count each sent's tokens, to avoid over div with pad,  shape: (batch_size * support_size, 1)
        token_counts = torch.sum(input_mask.contiguous().view(-1, mask_len), dim=1).unsqueeze(-1)
        sp_token_num = input_mask.shape[-1] - reps.shape[-2]  # num of [CLS], [SEP] tokens
        token_counts = token_counts - sp_token_num + 0.00001  # calculate pure token num and remove zero
        # mask pad-token's reps to 0 vectors [Notice that by default pad token's reps are not 0-vector]
        if sp_token_num == 2:
            trimed_mask = input_mask.narrow(-1, 1, reps.shape[-2]).float()  # remove mask of [CLS], [SEP]
        elif sp_token_num == 1:
            trimed_mask = input_mask.narrow(-1, 0, reps.shape[-2]).float()  # remove mask of [SEP]
        else:
            raise RuntimeError("Unexpected sp_token_num.")
        reps = reps * trimed_mask.unsqueeze(-1)
        # sum reps, shape (batch_size * support_size, emb_len)
        sum_reps = torch.sum(reps.contiguous().view(-1, sent_len, reps_size), dim=1)
        # averaged reps (batch_size, support_size, emb_len)
        # print("??????????",sum_reps.shape, token_counts.float())
        ave_reps = torch.div(sum_reps, token_counts.float()).contiguous().view(batch_size, support_size, reps_size)
        # print("========================",ave_reps.shape,ave_reps.unsqueeze(-2).shape)
        return ave_reps.unsqueeze(-2)

    def expand_test_item(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_input_mask: torch.Tensor,
            test_nwp_index: torch.Tensor,
            support_size: int,
    ) -> (torch.Tensor, torch.Tensor, torch.Tensor):
        return self.expand_it(test_token_ids, support_size), self.expand_it(test_segment_ids, support_size), \
               self.expand_it(test_input_mask, support_size), self.expand_it(test_nwp_index, support_size)

    def expand_it(self, item: torch.Tensor, support_size):
        expand_shape = list(item.unsqueeze_(1).shape)
        expand_shape[1] = support_size
        return item.expand(expand_shape)

    def cat_test_and_support(self, test_item, support_item):
        return torch.cat([test_item, support_item], dim=-1)

    def flatten_input(self, input_ids, segment_ids, input_mask):
        """ resize shape (batch_size, support_size, cat_len) to shape (batch_size * support_size, sent_len) """
        sent_len = input_ids.shape[-1]
        input_ids = input_ids.view(-1, sent_len)
        segment_ids = segment_ids.view(-1, sent_len)
        input_mask = input_mask.view(-1, sent_len)
        return input_ids, segment_ids, input_mask

    def flatten_index(self, nwp_index):
        """ resize shape (batch_size, support_size, index_len, 1) to shape (batch_size * support_size, index_len, 1) """
        nwp_sent_len = nwp_index.shape[-2]
        return nwp_index.contiguous().view(-1, nwp_sent_len, 1)

    def extract_non_word_piece_reps(self, reps, index):
        """
        Use the first word piece as entire word representation
        As we have only one index for each token, we need to expand to the size of reps dim.
        """
        expand_shape = list(index.shape)
        expand_shape[-1] = reps.shape[-1]  # expend index over embedding dim
        index = index.expand(expand_shape)
        nwp_reps = torch.gather(input=reps, index=index, dim=-2)  # extract over token level
        return nwp_reps

# 带label 语义
class BertSchemaContextEmbedder(BertContextEmbedder):
    def __init__(self, opt):
        super(BertSchemaContextEmbedder, self).__init__(opt)

    def forward(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj:torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor = None,
            support_segment_ids: torch.Tensor = None,
            support_adj:torch.Tensor = None,
            support_nwp_index: torch.Tensor = None,
            support_input_mask: torch.Tensor = None,
            reps_type: str = 'test_support',
    ) -> (torch.Tensor, torch.Tensor):
        """
        get context representation
        :param test_token_ids: (batch_size, test_len)
        :param test_segment_ids: (batch_size, test_len)
        :param test_nwp_index: (batch_size, test_len, 1)
        :param test_input_mask: (batch_size, test_len)
        :param support_token_ids: (batch_size, support_size, support_len)
        :param support_segment_ids: (batch_size, support_size, support_len)
        :param support_nwp_index: (batch_size, support_size, support_len, 1)
        :param support_input_mask: (batch_size, support_size, support_len)
        :param reps_type: select the reps type, default: reps for test and support tokens. Special choice is for label
        :return:
            if do concatenating representation:
                return (test_reps, support_reps, test_sent_reps, support_sent_reps):
                    test_reps, support_reps:  token reps (batch_size, support_size, nwp_sent_len, emb_len)
                    test_sent_reps, support_sent_reps: sent reps (batch_size, support_size, 1, emb_len)
            else do representation for a single sent (No support staff):
                return test_reps, shape is (batch_size, nwp_sent_len, emb_len)
        """
        if reps_type == 'test_support':
            if support_token_ids is not None:
                return self.concatenate_reps(
                    test_token_ids, test_segment_ids,test_adj, test_nwp_index, test_input_mask,
                    support_token_ids, support_segment_ids, support_adj,support_nwp_index, support_input_mask,
                )
            else:
                return self.single_reps(test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask,)
        elif reps_type == 'label':
            return self.get_label_reps(test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask)

    def get_label_reps(self, test_token_ids, test_segment_ids,test_adj, test_nwp_index, test_input_mask):
        batch_size = test_token_ids.shape[0]
        if self.opt.label_reps == 'cat':
            # todo: use label mask to represent a label with only in domain info
            _,reps = self.single_reps(test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask, )
        elif self.opt.label_reps in ['sep', 'sep_sum']:
            input_ids, segment_ids, input_mask = self.flatten_input(test_token_ids, test_segment_ids,
                                                                    test_input_mask)
            # nwp_index = self.flatten_index(test_nwp_index)
            # get flatten reps: shape (batch_size * label_num, label_des_len)
            sequence_output = self.embedder(input_ids, input_mask, segment_ids)[0]
            reps_size = sequence_output.shape[-1]
            if self.opt.label_reps == 'sep':  # use cls as each label's reps
                # re-shape to  (batch_size, label_num, label_des_len)
                reps = sequence_output.narrow(-2, 0, 1)  # fetch all [CLS] shape:(batch, 1, rep_size)
                reps = reps.contiguous().view(batch_size, -1, reps_size)
            elif self.opt.label_reps == 'sep_sum':  # average all label reps as reps
                reps = sequence_output
                emb_mask = self.expand_mask(test_input_mask, 2, reps_size)
                # todo: use mask to get sum of single embedding
                raise NotImplementedError
            else:
                raise ValueError("Wrong label_reps choice ")
        else:
            raise ValueError("Wrong reps_type choice")
        # print("==============++++",reps)
        return reps

    def expand_mask(self, item: torch.Tensor, expand_size, dim):
        new_item = item.unsqueeze(dim)
        expand_shape = list(new_item.shape)
        expand_shape[dim] = expand_size
        return new_item.expand(expand_shape)


class NormalContextEmbedder(ContextEmbedderBase):
    def __init__(self, opt, num_token):
        super(NormalContextEmbedder, self).__init__()
        self.opt = opt
        ''' load bert '''
        self.embedding_layer = torch.nn.Embedding(num_token, opt.emb_dim, padding_idx=0)
        self.lstm = torch.nn.LSTM(opt.emb_dim,opt.emb_dim)
    def forward(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor,
            support_segment_ids: torch.Tensor,
            support_nwp_index: torch.Tensor,
            support_input_mask: torch.Tensor,
    ) -> (torch.Tensor, torch.Tensor):
        """
        get context representation
        :param test_token_ids: (batch_size, test_len)
        :param test_segment_ids: (batch_size, test_len)
        :param test_nwp_index: (batch_size, test_len)
        :param test_input_mask: (batch_size, test_len)
        :param support_token_ids: (batch_size, support_size, support_len)
        :param support_segment_ids: (batch_size, support_size, support_len)
        :param support_nwp_index: (batch_size, support_size, support_len)
        :param support_input_mask: (batch_size, support_size, support_len)
        :return: (test_reps, support_reps, None, None):
            all their shape are (batch_size, support_size, nwp_sent_len, emb_len)
        """
        support_size = support_token_ids.shape[1]
        test_len = test_token_ids.shape[-1] - 2  # max len, exclude [CLS] and [SEP] token
        support_len = support_token_ids.shape[-1] - 1  # max len, exclude [SEP] token
        batch_size = support_token_ids.shape[0]
        ''' expand test input to shape: (batch_size, support_size, test_len)'''
        # test_token_ids, test_segment_ids, test_input_mask, test_nwp_index = self.expand_test_item(
        #     test_token_ids, test_segment_ids, test_input_mask, test_nwp_index, support_size)
        support_token_ids = support_token_ids.view(-1,support_len+1)

        ''' get reps '''
        test_reps = self.embedding_layer(test_token_ids)
        # print("?????????????",test_reps.shape)
        test_reps,_  = self.lstm(test_reps)
        support_reps = self.embedding_layer(support_token_ids)
        support_reps,_ = self.lstm(support_reps)
        # support_reps = support_reps.view(batch_size,support_size,support_len+ 1,-1)
        # print("???????????",support_len,support_reps.shape)
        return test_reps, support_reps, None, None

    def load_embedding(self):
        word2id = self.opt.word2id
        logging.info('Load embedding from pytorch-nlp.')
        if self.opt.embedding_cache:
            embedding_dict = GloVe(cache=self.opt.embedding_cache)  # load embedding cache from a specific place
        else:
            embedding_dict = GloVe(cache=self.opt.embedding_cache)  # load embedding cache from local dir or download now
        logging.info('Load embedding finished.')
        self.embedding_layer.weight.data.uniform_(-0.25, 0.25)
        # print("=================",embedding_dict)
        for word, idx in word2id.items():
            if word in embedding_dict.index_to_token:
                self.embedding_layer.weight.data[idx] = embedding_dict[word]
        logging.info('Word embedding size: {0}'.format(self.embedding_layer.weight.data.size()))

    def expand_test_item(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_input_mask: torch.Tensor,
            test_nwp_index: torch.Tensor,
            support_size: int,
    ) -> (torch.Tensor, torch.Tensor, torch.Tensor):
        return self.expand_it(test_token_ids, support_size), self.expand_it(test_segment_ids, support_size), \
               self.expand_it(test_input_mask, support_size), self.expand_it(test_nwp_index, support_size)

    def expand_it(self, item: torch.Tensor, support_size):
        expand_shape = list(item.unsqueeze_(1).shape)
        expand_shape[1] = support_size
        return item.expand(expand_shape)

    def flatten_input(self, input_ids):
        """ resize shape (batch_size, support_size, sent_len) to shape (batch_size * support_size, sent_len) """
        sent_len = input_ids.shape[-1]
        input_ids = input_ids.view(-1, sent_len)
        return input_ids


# Separately get two sent reps. 
class BertSeparateContextEmbedder(BertContextEmbedder):
    def __init__(self, opt):
        super(BertSeparateContextEmbedder, self).__init__(opt)
        self.gcns = nn.ModuleList()
        for i in range(opt.gcn_layers):
            gcn = GraphConvolution(in_features=opt.emb_dim,
                                   out_features=200,
                                   edge_types=3,
                                   dropout=0.3 if i != (opt.gcn_layers - 1) else None,
                                   use_bn=True)
            self.gcns.append(gcn)


    def forward(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj:torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor = None,
            support_segment_ids: torch.Tensor = None,
            support_adj:torch.Tensor = None,
            support_nwp_index: torch.Tensor = None,
            support_input_mask: torch.Tensor = None,
    ) -> (torch.Tensor, torch.Tensor):
        """
        get context representation
        :param test_token_ids: (batch_size, test_len)
        :param test_segment_ids: (batch_size, test_len)
        :param test_nwp_index: (batch_size, test_len, 1)
        :param test_input_mask: (batch_size, test_len)
        :param support_token_ids: (batch_size, support_size, support_len)
        :param support_segment_ids: (batch_size, support_size, support_len)
        :param support_nwp_index: (batch_size, support_size, support_len, 1)
        :param support_input_mask: (batch_size, support_size, support_len)
        :return: if do concatenating representation:
                return (test_reps, support_reps, test_sent_reps, support_sent_reps):
                    test_reps, support_reps:  token reps (batch_size, support_size, nwp_sent_len, emb_len)
                    test_sent_reps, support_sent_reps: sent reps (batch_size, support_size, 1, emb_len)
            else do representation for a single sent (No support staff):
                return test_reps, shape is (batch_size, nwp_sent_len, emb_len)
        """
        if support_token_ids is not None:
            return self.separate_reps(
                test_token_ids, test_segment_ids, test_adj, test_nwp_index, test_input_mask,
                support_token_ids, support_segment_ids, support_adj,support_nwp_index, support_input_mask,
            )
        else:
            return self.single_reps(test_token_ids, test_segment_ids,test_adj, test_nwp_index, test_input_mask,)
    # @torchsnooper.snoop()  
    def separate_reps(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj:torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor = None,
            support_segment_ids: torch.Tensor = None,
            support_adj:torch.Tensor = None,
            support_nwp_index: torch.Tensor = None,
            support_input_mask: torch.Tensor = None,
            reps_type: str = None,
    ) -> (torch.Tensor, torch.Tensor):
        """ Separately get two sent reps. """
        support_size = support_token_ids.shape[1]
        test_len = test_token_ids.shape[-1] - 2  # max len, exclude [CLS] and [SEP] token
        support_len = support_token_ids.shape[-1] - 1  # max len, exclude [SEP] token
        batch_size = support_token_ids.shape[0]
        ''' flatten input '''
        support_token_ids, support_segment_ids, support_input_mask = self.flatten_input(
            support_token_ids, support_segment_ids, support_input_mask)
        support_nwp_index = self.flatten_index(support_nwp_index)
        ''' get bert reps '''
        test_sequence_output = self.embedder(test_token_ids, test_input_mask, test_segment_ids)[0]
        support_sequence_output = self.embedder(support_token_ids, support_input_mask, support_segment_ids)[0]
        ''' extract reps '''
        # select pure sent part, remove [SEP] and [CLS], notice: seq_len1 == seq_len2 == max_len.
        test_reps = test_sequence_output.narrow(-2, 1, test_len)  # shape:(batch, test_len, rep_size)
        support_reps = support_sequence_output.narrow(-2, 1, support_len)  # shape:(batch * support_size, support_len, rep_size)
        support_adj = support_adj.contiguous().view(-1,3,support_len,support_len)
        test_reps_1 = test_reps
        support_reps_1 = support_reps
        if self.opt.gcns==1:
            for i in range(self.opt.gcn_layers):
                test_reps = self.gcns[i](test_reps, test_adj)
            for i in range(self.opt.gcn_layers):
                support_reps = self.gcns[i](support_reps, support_adj)
            # test_reps = 0.8 * test_reps + 0.2* test_reps_1
            # support_reps = 0.8* support_reps + 0.2 * support_reps_1
            test_reps = torch.cat((test_reps,test_reps_1),2)
            support_reps = torch.cat((support_reps,support_reps_1),2)

        # select non-word-piece tokens' representation
        nwp_test_reps = self.extract_non_word_piece_reps(test_reps, test_nwp_index)
        nwp_support_reps = self.extract_non_word_piece_reps(support_reps, support_nwp_index)
        # resize to shape (batch_size, support_size, sent_len, emb_len)
        reps_size = test_reps.shape[-1]
        nwp_test_reps = self.expand_it(nwp_test_reps, support_size).contiguous()
        nwp_support_reps = nwp_support_reps.view(batch_size, support_size, -1, reps_size).contiguous()
        # get whole sent reps
        # print("===========",support_reps.shape, support_input_mask.shape,test_input_mask.shape)
        # test_sent_reps = self.get_sent_reps(test_reps.unsqueeze(1), test_input_mask.unsqueeze(1))
        # support_sent_reps = self.get_sent_reps(support_reps.unsqueeze(1), support_input_mask.unsqueeze(1))
        return nwp_test_reps, nwp_support_reps,1,1

# # Separately get two sent reps. 
# class BertAttGRUSeparateContextEmbedder(BertContextEmbedder):
#     def __init__(self, opt):
#         super(BertAttGRUSeparateContextEmbedder, self).__init__(opt)

#     def forward(
#             self,
#             test_token_ids: torch.Tensor,
#             test_segment_ids: torch.Tensor,
#             test_nwp_index: torch.Tensor,
#             test_input_mask: torch.Tensor,
#             support_token_ids: torch.Tensor = None,
#             support_segment_ids: torch.Tensor = None,
#             support_nwp_index: torch.Tensor = None,
#             support_input_mask: torch.Tensor = None,
#     ) -> (torch.Tensor, torch.Tensor):
#         """
#         get context representation
#         :param test_token_ids: (batch_size, test_len)
#         :param test_segment_ids: (batch_size, test_len)
#         :param test_nwp_index: (batch_size, test_len, 1)
#         :param test_input_mask: (batch_size, test_len)
#         :param support_token_ids: (batch_size, support_size, support_len)
#         :param support_segment_ids: (batch_size, support_size, support_len)
#         :param support_nwp_index: (batch_size, support_size, support_len, 1)
#         :param support_input_mask: (batch_size, support_size, support_len)
#         :return: if do concatenating representation:
#                 return (test_reps, support_reps, test_sent_reps, support_sent_reps):
#                     test_reps, support_reps:  token reps (batch_size, support_size, nwp_sent_len, emb_len)
#                     test_sent_reps, support_sent_reps: sent reps (batch_size, support_size, 1, emb_len)
#             else do representation for a single sent (No support staff):
#                 return test_reps, shape is (batch_size, nwp_sent_len, emb_len)
#         """
#         if support_token_ids is not None:
#             return self.separate_reps(
#                 test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask,
#                 support_token_ids, support_segment_ids,support_adj, support_nwp_index, support_input_mask,
#             )
#         else:
#             return self.single_reps(test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask,)

#     def separate_reps(
#             self,
#             test_token_ids: torch.Tensor,
#             test_segment_ids: torch.Tensor,
#             test_nwp_index: torch.Tensor,
#             test_input_mask: torch.Tensor,
#             support_token_ids: torch.Tensor = None,
#             support_segment_ids: torch.Tensor = None,
#             support_nwp_index: torch.Tensor = None,
#             support_input_mask: torch.Tensor = None,
#             reps_type: str = None,
#     ) -> (torch.Tensor, torch.Tensor):
#         """ Separately get two sent reps. """
#         support_size = support_token_ids.shape[1]
#         test_len = test_token_ids.shape[-1] - 2  # max len, exclude [CLS] and [SEP] token
#         support_len = support_token_ids.shape[-1] - 1  # max len, exclude [SEP] token
#         batch_size = support_token_ids.shape[0]
#         ''' flatten input '''
#         support_token_ids, support_segment_ids, support_input_mask = self.flatten_input(
#             support_token_ids, support_segment_ids, support_input_mask)
#         support_nwp_index = self.flatten_index(support_nwp_index)
#         ''' get bert reps '''
#         test_sequence_output = self.embedder(test_token_ids, test_input_mask, test_segment_ids)[0]
#         support_sequence_output = self.embedder(support_token_ids, support_input_mask, support_segment_ids)[0]
#         ''' extract reps '''
#         # select pure sent part, remove [SEP] and [CLS], notice: seq_len1 == seq_len2 == max_len.
#         test_reps = test_sequence_output.narrow(-2, 1, test_len)  # shape:(batch, test_len, rep_size)
#         support_reps = support_sequence_output.narrow(-2, 1, support_len)  # shape:(batch * support_size, support_len, rep_size)
#         # select non-word-piece tokens' representation
#         nwp_test_reps = self.extract_non_word_piece_reps(test_reps, test_nwp_index)
#         nwp_support_reps = self.extract_non_word_piece_reps(support_reps, support_nwp_index)
#         # resize to shape (batch_size, support_size, sent_len, emb_len)
#         reps_size = test_reps.shape[-1]
#         nwp_test_reps = self.expand_it(nwp_test_reps, support_size).contiguous()
#         nwp_support_reps = nwp_support_reps.view(batch_size, support_size, -1, reps_size).contiguous()
#         # get whole sent reps
#         # print("===========",support_reps.shape, support_input_mask.shape,test_input_mask.shape)
#         # test_sent_reps = self.get_sent_reps(test_reps.unsqueeze(1), test_input_mask.unsqueeze(1))
#         support_sent_reps = self.get_sent_reps(support_reps.unsqueeze(1), support_input_mask.unsqueeze(1))

#         return nwp_test_reps, nwp_support_reps, test_reps, support_sent_reps


class BertSchemaSeparateContextEmbedder(BertSeparateContextEmbedder, BertSchemaContextEmbedder):
    def __init__(self, opt):
        super(BertSchemaSeparateContextEmbedder, self).__init__(opt)

    def forward(
            self,
            test_token_ids: torch.Tensor,
            test_segment_ids: torch.Tensor,
            test_adj:torch.Tensor,
            test_nwp_index: torch.Tensor,
            test_input_mask: torch.Tensor,
            support_token_ids: torch.Tensor = None,
            support_segment_ids: torch.Tensor = None,
            support_adj:torch.Tensor = None,
            support_nwp_index: torch.Tensor = None,
            support_input_mask: torch.Tensor = None,
            reps_type: str = 'test_support',
    ) -> (torch.Tensor, torch.Tensor):
        """
        get context representation
        :param test_token_ids: (batch_size, test_len)
        :param test_segment_ids: (batch_size, test_len)
        :param test_nwp_index: (batch_size, test_len, 1)
        :param test_input_mask: (batch_size, test_len)
        :param support_token_ids: (batch_size, support_size, support_len)
        :param support_segment_ids: (batch_size, support_size, support_len)
        :param support_nwp_index: (batch_size, support_size, support_len, 1)
        :param support_input_mask: (batch_size, support_size, support_len)
        :param reps_type: fit schema method
        :return: if do concatenating representation:
                return (test_reps, support_reps, test_sent_reps, support_sent_reps):
                    test_reps, support_reps:  token reps (batch_size, support_size, nwp_sent_len, emb_len)
                    test_sent_reps, support_sent_reps: sent reps (batch_size, support_size, 1, emb_len)
            else do representation for a single sent (No support staff):
                return test_reps, shape is (batch_size, nwp_sent_len, emb_len)
        """
        batch_size = test_token_ids.shape[0]
        if reps_type == 'test_support':
            if support_token_ids is not None:
                return self.separate_reps(
                    test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask,
                    support_token_ids, support_segment_ids,support_adj, support_nwp_index, support_input_mask,
                )
            else:
                return self.single_reps(test_token_ids, test_segment_ids,test_adj, test_nwp_index, test_input_mask, )
        elif reps_type == 'label':
            return self.get_label_reps(test_token_ids, test_segment_ids, test_adj,test_nwp_index, test_input_mask)


class ElectraContextEmbedder(BertContextEmbedder):
    """ Electra based context embedder """
    def __init__(self, opt):
        super(ElectraContextEmbedder, self).__init__(opt)

    def build_embedder(self):
        """ Load pretrained params """
        return ElectraModel.from_pretrained(self.opt.bert_path)


class ElectraSchemaContextEmbedder(BertSchemaContextEmbedder):
    """ Electra based Context Embedder with schema info """
    def __init__(self, opt):
        super(ElectraSchemaContextEmbedder, self).__init__(opt)

    def build_embedder(self):
        """ Load pretrained params """
        return ElectraModel.from_pretrained(self.opt.bert_path)
