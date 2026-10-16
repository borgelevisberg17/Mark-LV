# Visual Studio: geração e inserção de imagens

O Mark-LV expõe a action `visual_studio`, descoberta automaticamente por `core/action_loader.py`.

## Operações

- `generate`: cria uma imagem a partir de uma descrição.
- `edit`: transforma uma imagem existente, combinando `image_path` com a descrição das alterações.
- `insert`: insere uma imagem existente num documento Word ou numa apresentação PowerPoint.
- `generate` e `edit` também aceitam `target_file_path` para inserir o resultado na mesma operação.

## Inserção em documentos

- **DOCX:** cria uma cópia do documento e acrescenta uma página de capa no início.
- **PPTX:** por omissão, acrescenta um diapositivo visual de página inteira. Com `slide_index`, coloca a imagem no diapositivo existente indicado.
- O ficheiro original é preservado; os resultados usam nomes novos e únicos.
- A integração de EPUB não altera um EPUB já existente. Para um novo livro, gerar a capa e usá-la em `office_editor` com `create_epub` e `cover_image_path`.

## Configuração

A action reutiliza `config/api_keys.json` e a chave `gemini_api_key` já usada pela aplicação. Também aceita `GEMINI_API_KEY` como fallback. Não se deve colocar a chave no prompt, em argumentos da action, nem em logs.

Modelo predefinido: `gemini-2.5-flash-image`. É possível indicar `model` explicitamente se a conta tiver acesso a outro modelo compatível.

## Limites e validação

- Entradas suportadas para edição/inserção: PNG, JPEG e WEBP.
- Limite da imagem de origem: 20 MiB; limite do resultado: 30 MiB.
- DOCX/PPTX de destino limitados a 100 MiB.
- As imagens geradas são verificadas com Pillow; os pacotes Office são reabertos como ZIP e verificados antes de guardar.
- A geração exige rede, uma chave Gemini válida, quota e acesso ao modelo. Os testes automáticos não fazem chamadas pagas à API.
- A inserção é baseada em bibliotecas locais e pode não preservar recursos Office fora do que estas bibliotecas suportam. Validar documentos importantes na aplicação Office de destino.

## Exemplos de pedidos

- “Cria uma capa editorial para um livro sobre empreendedorismo em Angola e insere-a no ficheiro `livro.docx`.”
- “Gera um cartaz profissional para um evento de tecnologia e adiciona-o como novo diapositivo a `apresentacao.pptx`.”
- “Edita `logo.png` para uma versão monocromática, mantendo a composição, e guarda o resultado.”
- “Insere `ilustracao.png` no diapositivo 2 de `apresentacao.pptx`.”
