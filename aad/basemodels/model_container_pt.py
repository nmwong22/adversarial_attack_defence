
"""
This module implements the base class for PyTorch model container.
"""
import copy
import logging
import os
import time

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from ..datasets import DataContainer
from ..utils import name_handler, swap_image_channel

logger = logging.getLogger(__name__)


class ModelContainerPT:
    def __init__(self, model, data_container):
        assert isinstance(model, nn.Module), \
            f'Expecting a Torch Module, got {type(model)}'
        self.model = model
        assert isinstance(data_container, DataContainer), \
            f'Expecting a DataContainer, got {type(data_container)}'
        self.data_container = data_container

        self.device = torch.device(
            'cuda' if torch.cuda.is_available() else 'cpu')
        self.model.to(self.device)
        if self.device == 'cpu':
            logger.warning('GPU is not supported!')
        logger.debug('Using device: %s', self.device)

        # to allow the model train multiple times
        self.loss_train = []
        self.loss_test = []
        self.accuracy_train = []
        self.accuracy_test = []

    def fit(self, epochs=5, batch_size=64):
        since = time.time()

        self._fit_torch(epochs, batch_size)

        time_elapsed = time.time() - since
        logger.info('Time to complete training: %dm %.3fs',
                    int(time_elapsed // 60), time_elapsed % 60)

    def save(self, filename, overwrite=False):
        filename = os.path.join('save', filename)
        filename = name_handler(filename, 'pt', overwrite)

        torch.save(self.model.state_dict(), filename)

        logger.info('Saved model to %s', filename)

    def load(self, filename):
        self.model.load_state_dict(torch.load(
            filename, map_location=self.device))

        logger.info('Loaded model from %s', filename)

    def get_score(self, x, batch_size=128):
        if len(x) == 0:
            return np.array([], dtype=np.int64)

        if not isinstance(x, torch.Tensor):
            if (self.data_container.data_type == 'image'
                    and x.shape[1] not in (1, 3)):
                x = swap_image_channel(x)
            x = torch.from_numpy(x).float()

        # DataLoader is required to avoid overload the memory in GPU
        dataset = TensorDataset(x)
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False)
        num_classes = self.data_container.num_classes
        scores = np.zeros((len(x), num_classes), dtype=np.float32)

        start = 0
        self.model.eval()
        with torch.no_grad():
            for data in dataloader:
                xx = data[0].to(self.device)
                n = len(xx)
                score = self.model(xx).cpu().detach().numpy()
                scores[start: start+n] = score
                start += n
        return scores

    def predict(self, x, require_score=False, batch_size=128):
        if len(x) == 0:
            return np.array([], dtype=np.int64)

        if not isinstance(x, torch.Tensor):
            # only swap the channel when it is wrong!
            if (self.data_container.data_type == 'image'
                    and x.shape[1] not in (1, 3)):
                x = swap_image_channel(x)
            x = torch.from_numpy(x).float()

        # DataLoader is required to avoid overload the memory in GPU
        dataset = TensorDataset(x)
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False)
        num_classes = self.data_container.num_classes
        scores = np.zeros((len(x), num_classes), dtype=np.float32)
        predictions = -np.ones((len(x)), dtype=np.int64)

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        start = 0
        self.model.eval()
        with torch.no_grad():
            for data in dataloader:
                xx = data[0].to(self.device)
                n = len(xx)
                score = self.model(xx).cpu().detach().numpy()
                scores[start: start+n] = score
                predictions[start: start+n] = np.argmax(score, axis=1)
                start += n

        if require_score:
            return predictions, scores
        return predictions

    def predict_one(self, x, require_score=False):
        if len(x) == 0:
            return np.array([], dtype=np.int64)

        if isinstance(x, np.ndarray):
            x = np.expand_dims(x, axis=0)
        elif isinstance(x, torch.Tensor):
            x = x.unsqueeze(dim=0)
        else:
            raise TypeError(f'Got {type(x)}. Except a ndarray or tensor')
        prediction, output = self.predict(x, True)
        if require_score:
            return prediction.squeeze(), output.squeeze()
        else:
            return prediction.squeeze()

    def evaluate(self, x, labels):
        if len(x) == 0:
            return 0.0

        predictions = self.predict(x)
        accuracy = np.sum(np.equal(predictions, labels)) / len(labels)
        return accuracy

    def _fit_torch(self, epochs, batch_size):
        train_loader = self.data_container.get_dataloader(
            batch_size, is_train=True)
        test_loader = self.data_container.get_dataloader(
            batch_size, is_train=False)

        # save temporary state
        best_model_state = copy.deepcopy(self.model.state_dict())
        best_acc = 0.0

        # parameters are passed as dict, so it allows different optimizer
        params = self.model.optim_params
        optimizer = self.model.optimizer(self.model.parameters(), **params)

        # scheduler is optional
        if self.model.scheduler:
            scheduler_params = self.model.scheduler_params
            scheduler = self.model.scheduler(optimizer, **scheduler_params)

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        for epoch in range(epochs):
            time_start = time.time()

            tr_loss, tr_acc = self._train_torch(optimizer, train_loader)
            va_loss, va_acc = self._validate_torch(test_loader)
            if self.model.scheduler:
                scheduler.step()

            time_elapsed = time.time() - time_start
            logger.debug(
                '[%d/%d]%dm %.3fs: Train loss: %f acc: %f - Test loss: %f acc: %f',
                epoch+1, epochs,
                int(time_elapsed // 60), time_elapsed % 60,
                tr_loss, tr_acc, va_loss, va_acc)

            # save best state
            if va_acc >= best_acc:
                best_acc = va_acc
                best_model_state = copy.deepcopy(self.model.state_dict())

            # save logs
            self.loss_train.append(tr_loss)
            self.loss_test.append(va_loss)
            self.accuracy_train.append(tr_acc)
            self.accuracy_test.append(va_acc)

            # early stopping
            if (tr_acc >= 0.999 and va_acc >= 0.999) or tr_loss < 1e-4:
                logger.debug(
                    'Satisfied the accuracy threshold. Abort at %d epoch!',
                    epoch)
                break

        self.model.load_state_dict(best_model_state)

    def _train_torch(self, optimizer, loader):
        self.model.train()
        total_loss = 0.
        corrects = 0.
        loss_fn = self.model.loss_fn

        for x, y in loader:
            x = x.to(self.device)
            y = y.to(self.device)
            batch_size = x.size(0)

            optimizer.zero_grad()
            output = self.model(x)
            loss = loss_fn(output, y)

            loss.backward()
            optimizer.step()

            # for logging
            total_loss += loss.cpu().item() * batch_size
            pred = output.max(1, keepdim=True)[1]
            corrects += pred.eq(y.view_as(pred)).sum().cpu().item()

        n = len(loader.dataset)
        total_loss = total_loss / n
        acc = corrects / n
        return total_loss, acc

    def _validate_torch(self, loader):
        self.model.eval()
        total_loss = 0.
        corrects = 0
        loss_fn = self.model.loss_fn

        with torch.no_grad():
            for x, y in loader:
                x = x.to(self.device)
                y = y.to(self.device)
                batch_size = x.size(0)
                output = self.model(x)
                loss = loss_fn(output, y)
                total_loss += loss.cpu().item() * batch_size
                pred = output.max(1, keepdim=True)[1]
                corrects += pred.eq(y.view_as(pred)).sum().cpu().item()

        n = len(loader.dataset)
        total_loss = total_loss / n
        accuracy = corrects / n
        return total_loss, accuracy
