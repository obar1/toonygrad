sudo apt-get -y install --no-install-recommends clang python3-clang

PYTHONPATH="." VIZ=1 CLANG=1 pytest-watch

PYTHONPATH="." VIZ=1 CLANG=1 ./simple_test.py
