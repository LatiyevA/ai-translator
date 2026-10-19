import logging
from pathlib import Path

import typer
from openai import OpenAI

from ai_translator.config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

def main(input_path: str, output_path: str = "translated.txt"):
    """
    Translates text from the 'input_file', optionaly with a --output_file.
    """
    logging.basicConfig(filename="ai_translator", level=logging.INFO)
    logger.info("Starting AI Translator...")

    input_file = Path(input_path)
    output_file = Path(output_path)

    with open(input_file, "r") as f:
        text_to_translate = f.read()

    # client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI client initialized successfully.")

if __name__ == "__main__":
    typer.run(main)