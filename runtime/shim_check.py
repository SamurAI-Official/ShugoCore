import sys
print('version', sys.version.split()[0])
print('exe', sys.executable)
try:
    import pip
    print('pip', pip.__version__)
except Exception as exc:
    print('no pip:', exc)
try:
    import requests
    print('requests', requests.__version__)
except Exception as exc:
    print('no requests:', exc)
