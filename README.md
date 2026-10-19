# AI Translator

## What this project about

This is AI Translator that translates text from Chinese to Russian. This project is made by hand with minimal use of AI tools for learning purposes. The first version of the translator will translate text from Chinese to Russian using OpenAI API key. The project will take input and give output in .txt format, however, in the future, it is planned to support other formats and user's OpenAI subscription instead of API key.

## Plans

### To Do List

- [ ] Implement the basic translation of .txt files
- [ ] Implement the prompting for giving clues about context
- [ ] Implement glossary support for better translation (automatically created glossary with names)
- [ ] Implement support for user's OpenAI subscription
- [ ] Implement support for other file formats for input and output (e.g., .docx, .fb2, .epub, .webp)
- [ ] Implement multi-threaded translation for faster processing of large files
- [ ] Implement a user-friendly interface for easier usage

### Implementation Details

At first, the .txt file with Chinese text will be read. Chinese text will be divided into equal parts using tiktoken and langchain-text-splitters packages, those part then will be sent to the OpenAI model for getting individual glossary in json format for each part that will be stored in a SQLite database. After all parts are processed, all version of glossary will be merged into one final glossary that will be used for the final translation of the whole text. The translation also will be done in parts, with each part being translated in a distinct thread with the help of the glossary and a global prompt. Results of the fished translations will be stored in SQLite until all parts are translated. The final translation will be saved in a .txt file.