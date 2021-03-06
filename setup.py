"""
API for Digital Marketplace.
"""

from setuptools import setup, find_packages

setup(
    name='dto-digitalmarketplace-api',
    version='500',  # TODO: Use semver?
    url='https://github.com/arenanetworks/dto-digitalmarketplace-api',
    license='MIT',
    author='GDS Developers',
    description='Marketplace API',
    long_description=__doc__,
    packages=find_packages(),
    include_package_data=True,
    package_data={'app': ['DB/*']},
    zip_safe=False,
    install_requires=[
        'dto-digitalmarketplace-utils',
        'dto-digitalmarketplace-apiclient',
        'Flask',
        'Flask-Bcrypt',
        'Flask-SQLAlchemy<2.2',
        'psycopg2',
        'SQLAlchemy',
        'SQLAlchemy-Utils',
        'streql',
        'newrelic',
        'celery',
        'pycurl',
        'mailchimp3',

        # For schema validation
        'jsonschema',
        'rfc3987',
        'strict-rfc3339',

        # For the import script
        'requests',
        'docopt',
        'six',

        'pendulum',
        'jira',
        'pyjwt',

        'markdown',
	    'sshtunnel',
        'paramiko',
        'csvx'
    ]
)
