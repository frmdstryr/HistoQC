from setuptools import setup, find_packages

setup(
    name='histoqc',
    version='1.0',
    description='HistoQC is an open-source quality control tool for digital pathology slides ',
    long_description=open('Readme.md').read(),
    long_description_content_type='text/markdown',
    packages=find_packages(),
    include_package_data=True,
    package_data={'histoqc': [
        '*.ini',
        'UserInterface/*',
        'UserInterface/*/*',
        'UserInterface/*/*/*',
        'UserInterface/*/*/*/*',
        'AnnotationModule/*',
    ]},
    install_requires=['openslide-python', 'scikit-image', 'scikit-learn', 'numpy', 'scipy', 'matplotlib'],
    url='https://github.com/choosehappy/HistoQC',
)
