# Testing

```sh
.venv/bin/python -m unittest discover src/tests/ -v
```

Standard library `unittest` only. The model, audio, notifications, clipboard and paste tools are mocked, so no microphone, model download or graphical session is needed.
