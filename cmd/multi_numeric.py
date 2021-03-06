"""
Full pipeline for testing adversarial defences

The following steps will repeat 100 times:
1) Build and train the model
2) Generate adversarial examples
3) Train the defensive model
4) Blocking adversarial examples with the defensive model
"""
import argparse as ap
import json
import logging
import os
import time

import numpy as np

from aad.attacks import BIMContainer, get_attack
from aad.basemodels import BCNN, IrisNN, ModelContainerPT
from aad.defences import (AdversarialTraining, ApplicabilityDomainContainer,
                          DistillationContainer, FeatureSqueezing)
from aad.utils import get_time_str, name_handler
from cmd_utils import get_data_container, set_logging

LOG_NAME = 'Mul'
logger = logging.getLogger(LOG_NAME)

DIR_PATH = os.path.dirname(os.path.realpath(__file__))

# We don't reset random seed in every run
MAX_ITERATIONS = 100
BATCH_SIZE = 64  # 128 has some funny effects when the training set is just above it, eg: 150
TITLE_RESULTS = [
    'Index',
    'Clean:AdvTraining',
    'Clean:Destillation',
    'Clean:Squeezing',
    'Clean:AD',
    'FGSM:AdvTraining',
    'FGSM:Destillation',
    'FGSM:Squeezing',
    'FGSM:AD',
    'BIM:AdvTraining',
    'BIM:Destillation',
    'BIM:Squeezing',
    'BIM:AD',
    'DeepFool:AdvTraining',
    'DeepFool:Destillation',
    'DeepFool:Squeezing',
    'DeepFool:AD',
    'C&W:AdvTraining',
    'C&W:Destillation',
    'C&W:Squeezing',
    'C&W:AD',
]
ATTACK_LIST = ['Clean', 'FGSM', 'BIM', 'DeepFool', 'Carlini']
TITLE_ADV = ['Test'] + ATTACK_LIST
DEFENCE_LIST = ['AdvTraining', 'Destillation', 'Squeezing', 'AD']
ADV_TRAIN_RATIO = 0.25
SQUEEZER_FILTER_LIST = ['binary', 'normal']
SQUEEZER_DEPTH = 8
SQUEEZER_SIGMA = 0.2
AD_PARAM_FILE = os.path.join(DIR_PATH, 'AdParamsNumeral.json')


def block_attack(offset, advs, defence, def_name, blocked_res):
    for j in range(len(ATTACK_LIST)):
        adv = advs[j]
        blocked_indices = defence.detect(adv, return_passed_x=False)
        blocked_res[j*len(DEFENCE_LIST) + offset + 1] = len(blocked_indices)
        logger.info('%s blocks %d/%d samples on %s',
                    def_name, len(blocked_indices), len(adv), ATTACK_LIST[j])


def experiment(index, dname, max_epochs, adv_file, res_file):
    # STEP 1: select data
    dc = get_data_container(dname, use_shuffle=True, use_normalize=True)

    model = None
    if dname == 'BreastCancerWisconsin':
        model = BCNN()
        distill_model = BCNN()
    elif dname in ('BankNote', 'HTRU2', 'Iris', 'WheatSeed'):
        num_classes = dc.num_classes
        num_features = dc.dim_data[0]
        model = IrisNN(
            num_features=num_features,
            hidden_nodes=num_features*4,
            num_classes=num_classes
        )
        distill_model = IrisNN(
            num_features=num_features,
            hidden_nodes=num_features*4,
            num_classes=num_classes
        )
    if model is None:
        logger.error('Unrecognised dataset %s', dname)
    logger.info('Selected %s model', model.__class__.__name__)

    # STEP 2: train models
    mc = ModelContainerPT(model, dc)
    mc.fit(max_epochs=max_epochs, batch_size=BATCH_SIZE)
    accuracy = mc.evaluate(dc.x_test, dc.y_test)
    logger.info('Accuracy on test set: %f', accuracy)
    adv_res = [accuracy]

    # STEP 3: generate adversarial examples
    # no more than 1000 samples are required
    x = dc.x_test
    y = dc.y_test
    if len(x) > 1000:
        x = x[:1000]
        y = y[:1000]
    accuracy = mc.evaluate(x, y)
    adv_res.append(accuracy)

    advs = np.zeros((len(ATTACK_LIST), x.shape[0], x.shape[1]),
                    dtype=np.float32)
    pred_advs = -np.ones((len(ATTACK_LIST), len(y)),
                         dtype=np.int32)  # assign -1 as initial value
    pred_clean = mc.predict(x)

    advs[0] = x
    pred_advs[0] = pred_clean

    att_param_json = open(os.path.join(DIR_PATH, 'AttackParams.json'))
    att_params = json.load(att_param_json)

    for i, att_name in enumerate(ATTACK_LIST):
        # Clean set is only used in evaluation phase.
        if att_name == 'Clean':
            continue

        logger.debug('[%d]Running %s attack...', i, att_name)
        kwargs = att_params[att_name]
        logger.debug('%s params: %s', att_name, str(kwargs))
        Attack = get_attack(att_name)
        attack = Attack(mc, **kwargs)
        adv, pred_adv, x_clean, pred_clean_ = attack.generate(
            use_testset=False,
            x=x)
        assert np.all(pred_clean == pred_clean_)
        assert np.all(x == x_clean)
        logger.info('created %d adv examples using %s from %s',
                    len(advs[i]),
                    att_name,
                    dname)
        not_match = pred_adv != pred_clean
        success_rate = len(not_match[not_match == True]) / len(pred_clean)
        accuracy = mc.evaluate(adv, y)
        advs[i] = adv
        pred_advs[i] = pred_adv
        logger.info('Success rate of %s: %f', att_name, success_rate)
        logger.info('Accuracy on %s: %f', att_name, accuracy)
        adv_res.append(accuracy)
    adv_file.write(','.join([str(r) for r in adv_res]) + '\n')

    # STEP 4: train defences
    blocked_res = np.zeros(len(TITLE_RESULTS), dtype=np.int32)
    blocked_res[0] = index
    for def_name in DEFENCE_LIST:
        logger.debug('Running %s...', def_name)
        if def_name == 'AdvTraining':
            attack = BIMContainer(
                mc,
                eps=0.3,
                eps_step=0.1,
                max_iter=100,
                targeted=False)
            defence = AdversarialTraining(mc, [attack])
            defence.fit(max_epochs=max_epochs,
                        batch_size=BATCH_SIZE,
                        ratio=ADV_TRAIN_RATIO)
            block_attack(0, advs, defence, def_name, blocked_res)
        elif def_name == 'Destillation':
            if dname == 'Iris':
                temp = 10
            elif dname == 'BreastCancerWisconsin':
                temp = 2
            else:
                temp = 20
            defence = DistillationContainer(
                mc, distill_model, temperature=temp, pretrained=False)
            defence.fit(max_epochs=max_epochs, batch_size=BATCH_SIZE)
            block_attack(1, advs, defence, def_name, blocked_res)
        elif def_name == 'Squeezing':
            defence = FeatureSqueezing(
                mc,
                SQUEEZER_FILTER_LIST,
                bit_depth=SQUEEZER_DEPTH,
                sigma=SQUEEZER_SIGMA,
                pretrained=True,
            )
            defence.fit(max_epochs=max_epochs, batch_size=BATCH_SIZE)
            block_attack(2, advs, defence, def_name, blocked_res)
        elif def_name == 'AD':
            ad_param_file = open(AD_PARAM_FILE)
            # BreastCancer uses a different set of parameters
            if dname == 'BreastCancerWisconsin':
                param_file = os.path.join(DIR_PATH, 'AdParamsBC.json')
                ad_param_file = open(param_file)
            ad_params = json.load(ad_param_file)
            logger.debug('AD params: %s', str(ad_params))
            defence = ApplicabilityDomainContainer(
                mc,
                hidden_model=model.hidden_model,
                **ad_params)
            defence.fit()
            block_attack(3, advs, defence, def_name, blocked_res)

    res_file.write(','.join([str(r) for r in blocked_res]) + '\n')


def main():
    parser = ap.ArgumentParser()
    parser.add_argument(
        '-d', '--dataset', type=str, required=True,
        help='Name of the dataset')
    parser.add_argument(
        '-i', '--iteration', type=int, default=MAX_ITERATIONS,
        help='the number of iterations that the experiment will repeat')
    parser.add_argument(
        '-e', '--epoch', type=int, required=True,
        help='the number of max epochs for training')
    parser.add_argument(
        '-v', '--verbose', action='store_true', default=False,
        help='set logger level to debug')
    parser.add_argument(
        '-l', '--savelog', action='store_true', default=False,
        help='save logging file')
    parser.add_argument(
        '-w', '--overwrite', action='store_true', default=False,
        help='overwrite the existing file')

    # NOTE: the JSON file for parameter are hard coded.
    # We expect to run multiple attacks and defences in one iteration.
    args = parser.parse_args()
    dname = args.dataset
    max_iterations = args.iteration
    max_epochs = args.epoch
    verbose = args.verbose
    save_log = args.savelog
    overwrite = args.overwrite

    # set logging config. Run this before logging anything!
    set_logging(LOG_NAME, dname, verbose, save_log)

    print('[{}] Start experiment on {}...'.format(LOG_NAME, dname))
    logger.info('Start at    :%s', get_time_str())
    logger.info('RECEIVED PARAMETERS:')
    logger.info('dataset     :%s', dname)
    logger.info('iterations  :%d', max_iterations)
    logger.info('max_epochs  :%d', max_epochs)
    logger.info('verbose     :%r', verbose)
    logger.info('save_log    :%r', save_log)
    logger.info('overwrite   :%r', overwrite)

    result_file = name_handler(
        os.path.join('save', f'{LOG_NAME}_{dname}_i{max_iterations}'),
        'csv',
        overwrite=overwrite
    )

    adv_file = name_handler(
        os.path.join('save', f'{LOG_NAME}_{dname}_AdvExamples'),
        'csv',
        overwrite=overwrite
    )

    adv_file = open(adv_file, 'w')
    adv_file.write(','.join(TITLE_ADV) + '\n')
    res_file = open(result_file, 'w')
    res_file.write(','.join(TITLE_RESULTS) + '\n')
    for i in range(max_iterations):
        since = time.time()
        experiment(i, dname, max_epochs, adv_file, res_file)
        time_elapsed = time.time() - since
        print('Completed {} [{}/{}]: {:d}m {:2.1f}s'.format(
            dname,
            i+1,
            max_iterations,
            int(time_elapsed // 60),
            time_elapsed % 60))

    adv_file.close()
    res_file.close()


# Examples:
# python ./cmd/multi_numeric.py -vl -i 3 -e 200 -d Iris
# python ./cmd/multi_numeric.py -l -i 3 -e 200 -d Iris
# python ./cmd/multi_numeric.py -l -i 10 -e 200 -d BreastCancerWisconsin
if __name__ == '__main__':
    main()
    print(f'[{LOG_NAME}] Task completed!')
