from typing import List, Dict
import tqdm.notebook as tq
from tqdm.notebook import tqdm
import json
import pandas as pd
import numpy as np
import torch
from pathlib import Path
from torch.utils.data import DataLoader,Dataset
import pytorch_lightning as pl
import string
from pytorch_lightning.callbacks import ModelCheckpoint
from sklearn.model_selection import train_test_split
from transformers import (
    T5ForConditionalGeneration,
    T5TokenizerFast as T5Tokenizer,
    get_linear_schedule_with_warmup
    )


MODEL_NAME = 't5-base'
SOURCE_MAX_TOKEN_LEN = 512
TARGET_MAX_TOKEN_LEN = 64
SEP_TOKEN = '<sep>'
TOKENIZER_LEN = 32101 #after adding the new <sep> token
LEARNING_RATE = 2e-5


class QGModel(pl.LightningModule):
    def __init__(self):
        super().__init__()
        self.model = T5ForConditionalGeneration.from_pretrained(MODEL_NAME, return_dict=True)
        self.model.resize_token_embeddings(TOKENIZER_LEN) #resizing after adding new tokens to the tokenizer

    def forward(self, input_ids, attention_mask, labels=None):
        output = self.model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
        return output.loss, output.logits

    def training_step(self, batch, batch_idx):
        input_ids = batch['input_ids']
        attention_mask = batch['attention_mask']
        labels = batch['labels']
        loss, output = self(input_ids, attention_mask, labels)
        self.log('train_loss', loss, prog_bar=True, logger=True)
        return loss

    def validation_step(self, batch, batch_idx):
        input_ids = batch['input_ids']
        attention_mask = batch['attention_mask']
        labels = batch['labels']
        loss, output = self(input_ids, attention_mask, labels)
        self.log('val_loss', loss, prog_bar=True, logger=True)
        return loss

    def test_step(self, batch, batch_idx):
        input_ids = batch['input_ids']
        attention_mask = batch['attention_mask']
        labels = batch['labels']
        loss, output = self(input_ids, attention_mask, labels)
        self.log('test_loss', loss, prog_bar=True, logger=True)
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=LEARNING_RATE, betas=(0.9, 0.999), eps=1e-08)
        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            num_warmup_steps=0,
            num_training_steps=self.trainer.estimated_stepping_batches
        )
        return [optimizer], [{"scheduler": scheduler, "interval": "step"}]



class DistractorGenerator():
    def __init__(self):
        self.tokenizer = T5Tokenizer.from_pretrained(MODEL_NAME)
        self.tokenizer.add_tokens(SEP_TOKEN)
        self.tokenizer_len = len(self.tokenizer)
        checkpoint_path = 'app/models/mcq_question_models/app/models/distractor_generation/Model/Distractor-generation-checkpoints-v4.ckpt'
        self.dg_model = QGModel.load_from_checkpoint(checkpoint_path)
        self.dg_model.freeze()
        self.dg_model.eval()

    def generate(self, generate_count: int, correct: str, question: str, context: str) -> List[str]:
        
        generate_triples_count = int(generate_count / 3) + 1 
        
        model_output = self._model_predict(generate_triples_count, correct, question, context)

        cleaned_result = model_output.replace('<pad>', '').replace('</s>', '<sep>')
        cleaned_result = self._replace_all_extra_id(cleaned_result)
        distractors = cleaned_result.split('<sep>')[:-1]
        distractors = [x.translate(str.maketrans('', '', string.punctuation)) for x in distractors]
        distractors = list(map(lambda x: x.strip(), distractors))

        return distractors

    def _model_predict(self, generate_count: int, correct: str, question: str, context: str) -> str:
        source_encoding = self.tokenizer(
            '{} {} {} {} {}'.format(question, SEP_TOKEN, context, SEP_TOKEN, correct),
            max_length= SOURCE_MAX_TOKEN_LEN,
            padding='max_length',
            truncation= True,
            return_attention_mask=True,
            add_special_tokens=True,
            return_tensors='pt'
            )

        generated_ids = self.dg_model.model.generate(
            input_ids=source_encoding['input_ids'],
            attention_mask=source_encoding['attention_mask'],
            num_beams=generate_count,
            num_return_sequences=generate_count,
            max_length=TARGET_MAX_TOKEN_LEN,
            repetition_penalty=2.5,
            length_penalty=1.0,
            early_stopping=True,
            use_cache=True
        )

        preds = {
            self.tokenizer.decode(generated_id, skip_special_tokens=False, clean_up_tokenization_spaces=True)
            for generated_id in generated_ids
        }

        return ''.join(preds)

    def _correct_index_of(self, text:str, substring: str, start_index: int = 0):
        try:
            index = text.index(substring, start_index)
        except ValueError:
            index = -1

        return index

    def _replace_all_extra_id(self, text: str):
        new_text = text
        start_index_of_extra_id = 0

        while (self._correct_index_of(new_text, '<extra_id_') >= 0):
            start_index_of_extra_id = self._correct_index_of(new_text, '<extra_id_', start_index_of_extra_id)
            end_index_of_extra_id = self._correct_index_of(new_text, '>', start_index_of_extra_id)

            new_text = new_text[:start_index_of_extra_id] + '<sep>' + new_text[end_index_of_extra_id + 1:]

        return new_text