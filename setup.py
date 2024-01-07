import setuptools

#with open('requirements.txt') as f:
#    requirements = f.read().splitlines()
requirements = []
setuptools.setup(
    name="sess",
    version="0.0.1",
    packages=setuptools.find_packages(),
    install_requires=requirements,
)
