"""PyInstaller entry point; keep business imports behind desktop setup."""
import sys

from src.desktop.runtime import main


def packaging_self_test():
    """Exercise frozen native imports/converter without models, sockets or secrets."""
    from pathlib import Path
    import ctranslate2
    from ctranslate2.converters import TransformersConverter
    import faster_whisper
    import sacremoses
    import sentencepiece
    import torch
    from transformers import MarianMTModel, MarianTokenizer

    if not (Path(sys._MEIPASS) / "web/index.html").is_file():
        raise RuntimeError("Missing frozen Web resources")
    assert torch.tensor([1.0]).sum().item() == 1.0
    assert ctranslate2.get_supported_compute_types("cpu")
    assert all((TransformersConverter, faster_whisper.WhisperModel,
                sacremoses.MosesTokenizer, sentencepiece.SentencePieceProcessor,
                MarianMTModel, MarianTokenizer))
    print("Frozen backend import and native CPU smoke check passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--packaging-self-test"]:
        packaging_self_test()
    else:
        main()
