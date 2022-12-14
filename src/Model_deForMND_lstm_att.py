import random
import torch
import torch.nn as nn
from torch.autograd import Variable
import numpy as np
import torch.nn.functional as F
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# for comments: N for batch_size, M for max_len after pad, this seq2seq model is batch_first == True in lstm

class EncoderLSTM(nn.Module):
    def __init__(self, embedding_size=52, hidden_size=100):
        super(EncoderLSTM, self).__init__()
        self.hidden_size = hidden_size
        self.lstm = nn.LSTM(embedding_size, hidden_size, num_layers=1, batch_first=False, dropout=0.5)
        self.dropout = nn.Dropout(0.1)

    def forward(self, input):
        embedded = self.dropout(input)
        outputs, (hidden_state, cell_state) = self.lstm(embedded.float())
        return outputs, hidden_state, cell_state

    # def initHidden(self, batch_size):
    #     return torch.zeros(1, batch_size, self.hidden_size, device=device)

class Attention(nn.Module):
    def __init__(self, encoder_hidden_dim, decoder_hidden_dim):
        super().__init__()

        # The input dimension will the the concatenation of
        # encoder_hidden_dim (hidden) and  decoder_hidden_dim(encoder_outputs)
        self.attn_hidden_vector = nn.Linear(encoder_hidden_dim + decoder_hidden_dim, decoder_hidden_dim)

        # We need source len number of values for n batch as the dimension
        # of the attention weights. The attn_hidden_vector will have the
        # dimension of [source len, batch size, decoder hidden dim]
        # If we set the output dim of this Linear layer to 1 then the
        # effective output dimension will be [source len, batch size]
        self.attn_scoring_fn = nn.Linear(decoder_hidden_dim, 1, bias=False)

    def forward(self, hidden, encoder_outputs):
        # hidden = [1, batch size, decoder hidden dim]
        # encoder_outputs = [max_len, batch size, encoder hidden dim]
        max_len = encoder_outputs.shape[0]

        # We need to calculate the attn_hidden for each source words.
        # Instead of repeating this using a loop, we can duplicate
        # hidden src_len number of times and perform the operations.
        # Repeat didn't change the second and third dimension.
        hidden = hidden.repeat(max_len, 1, 1)

        # Calculate Attention Hidden values
        # ??????torch.cat((A,B),dim)?????????????????????dim???????????????????????????????????????????????????????????????
        # [max,N,hidden]
        attn_hidden = torch.tanh(self.attn_hidden_vector(torch.cat((hidden, encoder_outputs), dim=2)))

        # Calculate the Scoring function. Remove 3rd dimension.
        attn_scoring_vector = self.attn_scoring_fn(attn_hidden).squeeze(2)

        # The attn_scoring_vector has dimension of [source len, batch size]
        # Since we need to calculate the softmax per record in the batch
        # we will switch the dimension to [batch size,source len]
        attn_scoring_vector = attn_scoring_vector.permute(1, 0)

        # Softmax function for normalizing the weights to
        # probability distribution [batch size,source len]
        return F.softmax(attn_scoring_vector, dim=1)

class DecoderLSTM(nn.Module):
    def __init__(self, dict_size, embedding_size=52, encoder_hidden_size=100, decoder_hidden_size=100):
        super(DecoderLSTM, self).__init__()
        self.dict_size = dict_size
        self.decoder_hidden_size = decoder_hidden_size
        self.attention = Attention(encoder_hidden_size, decoder_hidden_size)
        self.embedding = nn.Embedding(dict_size, embedding_size) #(original) --> (originial,embedding_size)
        # Add the encoder_hidden_size and embedding_size
        self.lstm = nn.LSTM(encoder_hidden_size + embedding_size, decoder_hidden_size, num_layers=1, batch_first=False, dropout=0.5)
        # Combine all the features for better prediction
        self.linear = nn.Linear(encoder_hidden_size + decoder_hidden_size + embedding_size, dict_size) #other_size to dict_size;
        self.dropout = nn.Dropout(0.1)

    def forward(self, input, hidden_state, cell_state, encoder_outputs):
        embedded = self.dropout(self.embedding(input))
        # embedded = self.embedding(input)
        embedded = embedded.view(embedded.size(0), embedded.size(1), -1) # view as three dimension

        # Calculate the attention weights, [batch_size, max_len]==>[batch_size, 1, max_len]
        a = self.attention(hidden_state, encoder_outputs).unsqueeze(1)
        # We need to perform the batch wise dot product.
        # Hence need to shift the batch dimension to the front. [batch_size, max_len, encoder_hidden_size]
        encoder_outputs = encoder_outputs.permute(1, 0, 2)
        # Use PyTorch's bmm function to calculate the weight W.
        # torch.bmm((b,1,m),(b,m,h))==(b,1,en_h)
        W = torch.bmm(a, encoder_outputs)
        # Revert the batch dimension. (b,1,en_h)==(1,b,en_h)
        W = W.permute(1, 0, 2)
        # concatenate the previous output with W (1,b,1)+(1,b,en_h)
        rnn_input = torch.cat((embedded, W), dim=2)
        output = rnn_input
        # output = F.relu(rnn_input) error rate increase with relu
        output, (hidden_state, cell_state) = self.lstm(output.float(), (hidden_state.float(), cell_state.float()))
        # Remove the sentence length dimension and pass them to the Linear layer
        linear = self.linear(torch.cat((output, W, embedded), dim=2))

        # linear = self.linear(output)  # output.shape = [max_length, hidden_size], linear.shape = dict_size
        # linear.shape = [max_len, batch_size, len(dictionary)]
        # softmax = self.softmax(linear)  ## softmax.shape=linear.shape=[batch_size, max_len, len(dictionary)]
        softmax = linear
        return output, softmax, hidden_state, cell_state

    def initHidden(self, batch_size):
        return torch.zeros(1, batch_size, self.hidden_size, device=device) # first hidden_state 'sos' INDEX = 0 in batch

class AsrModel(nn.Module):
    def __init__(self, dict_size, max_length, sos, pad):
        super(AsrModel, self).__init__()

        self.dict_size = dict_size # == dict_size
        self.max_length = max_length
        self.encoder = EncoderLSTM()
        self.decoder = DecoderLSTM(self.dict_size)

        self.sos = sos #np.zeros((1, 1))
        self.pad = pad
        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=int(self.pad[0][0]))

    def forward(self, input, target, teacher_forcing_ratio: float = 0.5): #input.shape=(N,frames,F), target.shape=(N,M,1)
        # print('target.shape:', target.shape) #[100, 75, 1]
        batch_size = input.shape[1]
        # frame_num = input.shape[1]
        max_phn_num = target.shape[0]

        # EncoderLSTM
        encoder_outputs, encoder_hidden, encoder_cell = self.encoder.forward(input) #hidden_state is encoder last hidden of a batch of sentences
        # encoder hidden.shape = [1, N, hiddensize]
        # print('1enouts:', encoder_outputs.shape)
        # print('2enhid:', encoder_hidden.shape)
        # print('2encel:', encoder_cell.shape)

        # DecoderLSTM
        decoder_hidden = encoder_hidden
        decoder_cell = encoder_cell
        # self.dict_size: 63 phonemes
        # first input to the decoder is the <sos> token
        decoder_input = torch.unsqueeze(target[0,:,:],0)
        decoder_softmaxs = torch.zeros(max_phn_num, batch_size, self.decoder.dict_size, device=device)
        # [N,M,hidden/dict]
        # print('3detar:', target.shape)
        # print('3deinput:', decoder_input.shape)
        # print('3dehid:', decoder_hidden.shape)
        # print('3decel:', decoder_cell.shape)
        # Teacher forcing: Feed the target as the next input
        for iphn in range(max_phn_num):
            decoder_output, decoder_softmax, decoder_hidden, decoder_cell = self.decoder.forward(decoder_input, decoder_hidden, decoder_cell, encoder_outputs)
            # print('3deouts:',decoder_output.shape)
            # print('4desofmax:',decoder_softmax.shape)
            # print('5dehid:',decoder_hidden.shape)
            # print('5decel:', decoder_cell.shape)
            decoder_softmaxs[iphn,:,:] = torch.unsqueeze(decoder_softmax[0,:,:],0)  # [N,1,dict]
            teacher_forcing = random.random() < teacher_forcing_ratio # True or False; [0,1)<0 == False
            if iphn < (max_phn_num-1) and teacher_forcing == True:
                decoder_input = torch.tensor(target[iphn+1,:,:].tolist(), device=device) #teacher forcing
                # print('5deinput:',decoder_input.shape)
                decoder_input = torch.unsqueeze(decoder_input, 0)
                # print('6deinput:', decoder_input.shape)
            elif iphn < (max_phn_num-1) and teacher_forcing == False:
                decoder_input = torch.argmax(decoder_softmax, 2)[:, :, None]  # teacher forcing off
                # print('62deinput:', decoder_input.shape)

        # asr_outputs.shape = [N, M, 1] ## softmax.shape = [N, M, dict_size+tokens]
        asr_outputs = np.argmax(decoder_softmaxs[:-1,:,:].cpu().detach().numpy(), 2)[:, :, np.newaxis]
        softmax_cal, target_cal = decoder_softmaxs[:-1,:,:].reshape(-1, decoder_softmaxs.size(-1)), target[1:,:,:].reshape(-1)
        return softmax_cal, target_cal, asr_outputs
