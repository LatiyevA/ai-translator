# AI Translator from Chinese to Russian

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

Text will be divided into chapters or into equal parts of a certain size. At first cheap model will be used for creating and editing a glossary of the translation. Then human can check and edit the glossary. After that, the main translation will be done using a more expensive model with the help of the glossary. The translation will be done in parts, every chapter or section will be translated in a distinct thread with glossary and global prompt, then all parts will be combined into one file. Might consider simple sqllite database for storing the part translated parts before assembling them in the final output file. The final output will be a .txt file with the translated text.