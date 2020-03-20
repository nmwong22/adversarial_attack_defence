"""
This module implements the Applicability Domain based adversarial detection.
"""
import logging

import numpy as np
import sklearn.neighbors as knn
import torch
from torch.utils.data import DataLoader

from ..datasets import NumeralDataset
from .detector_container import DetectorContainer

logger = logging.getLogger(__name__)


class ApplicabilityDomainContainer(DetectorContainer):
    """
    Class performs adversarial detection based on Applicability Domain.
    """

    def __init__(self, model_container, hidden_model=None, k1=9, k2=12, confidence=0.8):
        """Create a class `ApplicabilityDomainContainer` instance.
        """
        super(ApplicabilityDomainContainer, self).__init__(model_container)

        params_received = {
            'k1': k1,
            'k2': k2,
            'confidence': confidence}
        self.params.update(params_received)
        self.device = model_container.device
        self.num_classes = model_container.data_container.num_classes

        if hidden_model is not None:
            self.hidden_model = hidden_model
        else:
            self.hidden_model = self.dummy_model

        # placeholders for the objects used by AD
        self.num_components = 0  # number of hidden components
        # keep track max for each class, size: [num_classes, num_components]
        self._x_max = None
        self._x_min = None
        self._s2_models = []  # in-class KNN models
        self._s3_model = None  # KNN models using training set
        self.k_means = np.zeros(self.num_classes, dtype=np.float32)
        self.k_stds = np.zeros_like(self.k_means)
        self.thresholds = np.zeros_like(self.k_means)
        self.encode_train_np = None
        self.y_train_np = None

    def fit(self):
        """
        Train the model using the training set from `model_container.data_container`.
        """
        if self.encode_train_np is not None:
            logger.warning(
                'You cannot call fit() method multiple times! Please start a new instance')
            return False

        self._log_time_start()

        # Step 1: compute hidden layer outputs from inputs
        dc = self.model_container.data_container
        x_train_np = dc.data_train_np
        self.encode_train_np = self._preprocessing(x_train_np)
        self.y_train_np = dc.label_train_np
        self.num_components = self.encode_train_np.shape[1]

        self._fit_stage1()
        self._fit_stage2()
        self._fit_stage3()

        self._log_time_end('train AD')
        return True

    def detect(self, adv):
        n = len(adv)
        # 1: passed test, 0: blocked by AD
        passed = np.ones(n, dtype=np.int8)

        # The defence does NOT know the true class of adversarial examples. It
        # computes predictions instead.
        pred_adv = self.model_container.predict(adv)

        # The adversarial examples exist in image/data space. The KNN model runs
        # in hidden layer (encoded space)
        encoded_adv = self._preprocessing(adv)

        passed = self._def_state1(encoded_adv, pred_adv, passed)
        blocked = len(passed[passed == 0])
        logger.info('Stage 1: blocked %d inputs', blocked)
        passed = self._def_state2(encoded_adv, pred_adv, passed)
        blocked = len(passed[passed == 0]) - blocked
        logger.info('Stage 2: blocked %d inputs', blocked)
        passed = self._def_state3(encoded_adv, pred_adv, passed)
        blocked = len(passed[passed == 0]) - blocked
        logger.info('Stage 3: blocked %d inputs', blocked)

        passed_indices = np.nonzero(passed)
        blocked_indices = np.delete(np.arange(n), passed_indices)
        return adv[passed_indices], blocked_indices

    def _preprocessing(self, x_np):
        dataset = NumeralDataset(torch.as_tensor(x_np))
        dataloader = DataLoader(
            dataset,
            batch_size=128,
            shuffle=False,
            num_workers=0)

        # run 1 sample to get size of output
        x, _ = next(iter(dataloader))
        x = x.to(self.device)
        outputs = self.hidden_model(x[:1])
        num_components = outputs.size()[1]  # number of hidden components

        x_encoded = torch.zeros(len(x_np), num_components)

        start = 0
        with torch.no_grad():
            for x, _ in dataloader:
                x = x.to(self.device)
                batch_size = len(x)
                x_out = self.hidden_model(x).view(batch_size, -1)  # flatten
                x_encoded[start: start+batch_size] = x_out
        return x_encoded.cpu().detach().numpy()

    def _fit_stage1(self):
        self._x_min = np.empty(
            (self.num_classes, self.num_components), dtype=np.float32)
        self._x_max = np.empty_like(self._x_min)

        for i in range(self.num_classes):
            indices = np.where(self.y_train_np == i)[0]
            x = self.encode_train_np[indices]
            self._x_max[i] = np.amax(x, axis=0)
            self._x_min[i] = np.amin(x, axis=0)

    def _fit_stage2(self):
        self._s2_models = []
        k1 = self.params['k1']
        zeta = self.params['confidence']
        for i in range(self.num_classes):
            indices = np.where(self.y_train_np == i)[0]
            x = self.encode_train_np[indices]
            # models are grouped by classes, y doesn't matter
            y = np.ones(len(x))
            model = knn.KNeighborsClassifier(n_neighbors=k1, n_jobs=-1)
            model.fit(x, y)
            self._s2_models.append(model)
            # number of neighbours is k + 1, since it will return the node itself
            dist, _ = model.kneighbors(x, n_neighbors=k1+1)
            avg_dist = np.sum(dist, axis=1) / float(k1)
            self.k_means[i] = np.mean(avg_dist)
            self.k_stds[i] = np.std(avg_dist)
            self.thresholds[i] = self.k_means[i] + zeta * self.k_stds[i]

    def _fit_stage3(self):
        x = self.encode_train_np
        y = self.y_train_np
        k2 = self.params['k2']
        self._s3_model = knn.KNeighborsClassifier(
            n_neighbors=k2,
            n_jobs=-1,
            weights='distance')
        self._s3_model.fit(x, y)

    def _def_state1(self, adv, pred_adv, passed):
        """
        A bounding box which uses [min, max] from traning set
        """
        for i in range(self.num_classes):
            indices = np.where(pred_adv == i)[0]
            x = adv[indices]
            i_min = self._x_min[i]
            i_max = self._x_max[i]
            blocked_indices = np.where(
                np.all(np.logical_or(x < i_min, x > i_max), axis=1)
            )[0]
            passed[blocked_indices] = 0
        return passed

    def _def_state2(self, adv, pred_adv, passed):
        """
        Filtering the inputs based on in-class k nearest neighbours.
        """
        indices = np.arange(len(adv))
        passed_indices = np.where(passed == 1)[0]
        passed_adv = adv[passed_indices]
        passed_pred = pred_adv[passed_indices]
        models = self._s2_models
        classes = np.arange(self.num_classes)
        k1 = self.params['k1']
        for model, threshold, c in zip(models, self.thresholds, classes):
            inclass_indices = np.where(passed_pred == c)[0]
            x = passed_adv[inclass_indices]
            neigh_dist, neigh_ind = model.kneighbors(
                x, n_neighbors=k1, return_distance=True)
            mean = np.mean(neigh_dist, axis=1)
            sub_blocked_indices = np.where(mean > threshold)[0]
            # trace back the original indices from input adversarial examples
            blocked_indices = indices[passed_indices][inclass_indices][sub_blocked_indices]
            passed[blocked_indices] = 0
        return passed

    def _def_state3(self, adv, pred_adv, passed):
        """
        Filtering the inputs based on k nearest neighbours with entire training set
        """
        indices = np.arange(len(adv))
        passed_indices = np.where(passed == 1)[0]
        passed_adv = adv[passed_indices]
        passed_pred = pred_adv[passed_indices]
        model = self._s3_model
        k2 = self.params['k2']
        knn_pred = model.predict(passed_adv)
        not_match_indices = np.where(np.not_equal(knn_pred, passed_pred))[0]
        blocked_indices = indices[passed_indices][not_match_indices]
        passed[blocked_indices] = 0
        return passed

    @staticmethod
    def dummy_model(x):
        """
        Return the input.Use this method when we don't need a hidden layer encoding.
        """
        return x
