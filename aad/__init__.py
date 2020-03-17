import logging

from aad import attacks, basemodels, datasets, defences

# Semantic Version
__version__ = '0.0.1'

LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'std': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            'datefmt': '%Y-%m-%d %H:%M'
        }
    },
    'handlers': {
        'default': {
            'class': 'logging.NullHandler',
        },
        'test': {
            'class': 'logging.StreamHandler',
            'formatter': 'std',
            'level': logging.INFO
        }
    },
    'loggers': {
        'art': {
            'handlers': ['default']
        },
        'tests': {
            'handlers': ['test'],
            'level': 'INFO',
            'propagate': True
        }
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)
