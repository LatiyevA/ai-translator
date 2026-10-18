import logging
from pathlib import Path

import typer
from openai import OpenAI

from ai_translator.config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

def main(input_file: str, output_file: str = "translated.txt"):
    """
    Translates text from the 'input_file', optionaly with a --output_file.
    """
    logging.basicConfig(filename="ai_translator", level=logging.INFO)
    logger.info("Starting AI Translator...")

    input_path = Path(input_file)
    output_path = Path(output_file)

    # client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("OpenAI client initialized successfully.")

if __name__ == "__main__":
    typer.run(main)