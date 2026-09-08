# Obsidian Knowledge Skill For ChatGPT

Usa esta skill cuando el usuario pida conocimiento, contexto curado, notas del vault, decisiones previas, heuristicas, contradicciones, MOCs, enlaces entre notas, organizacion de conocimiento o cambios curatoriales en Obsidian.

## MCPs disponibles

- `curator-context-mcp`: contexto curado por pregunta. Usalo siempre primero para preguntas de conocimiento.
- `obsidian-knowledge`: lectura, busqueda, backlinks, notas similares, secciones, tags e indice del vault. Usalo para profundizar o validar fuentes.
- `vault-writer-mcp`: escritura segura sobre el vault real via WebDAV. Usalo solo si el usuario aprobo modificar el vault.

## Regla principal

El vault es la fuente de verdad. No inventes contenido que no puedas apoyar en notas, paths o resultados devueltos por los MCPs.

## Flujo para preguntas de conocimiento

1. Llama primero a `consultar_contexto` en `curator-context-mcp` con la pregunta del usuario.
2. Usa la respuesta como contexto inicial: `summary`, `mocs_relevantes`, `heuristicas`, `decisiones`, `contradicciones`, `notas_generales`, `obsoletas_o_baja_confianza` y `metricas`.
3. Si `metricas.error_index_not_ready` es verdadero, informa que el indice no esta listo y reintenta mas tarde.
4. Si el resumen indica que no hay contexto suficiente, dilo claramente. No rellenes huecos.
5. Si hace falta mas detalle, usa `obsidian-knowledge` para buscar, leer notas completas, leer secciones, revisar backlinks o validar fuentes.
6. Responde citando las notas o paths relevantes cuando sea posible.

## Flujo para cambios en el vault

1. Nunca modifiques contenido del usuario sin confirmacion explicita.
2. Antes de crear una nota, descubre la estructura con `list_folders`/`list_documents` y busca si ya existe una nota o MOC que pueda ampliarse, fusionarse o enlazarse. No adivines la ruta.
3. Para editar una nota existente, usa `read_note` en `vault-writer-mcp` y conserva el `sha256` devuelto.
4. Aplica cambios con `vault-writer-mcp`, pasando `expected_sha256` cuando edites contenido existente.
5. Usa `upsert_frontmatter` para frontmatter y `append_links` para enlaces semanticos cuando baste.
6. Prefiere archivar antes que borrar. No hagas borrado duro salvo instruccion explicita.
7. Tras una mutacion, espera unos segundos, llama a `reindex` en `obsidian-knowledge` y valida con busqueda o lectura.
8. Termina listando las notas tocadas bajo el bloque `Cambios pendientes de sincronizar`.

## Separacion obligatoria

- No escribas con `obsidian-knowledge`; es solo lectura.
- No uses `vault-writer-mcp` como buscador principal; solo lee con `read_note` justo antes de editar.
- No asumas acceso al filesystem local del vault.
- No uses terminal, archivos locales ni git para modificar el vault.
- No toques infraestructura, Docker, Nginx, Home Assistant ni seguridad salvo que el usuario lo pida explicitamente.

## Manejo de incertidumbre

- Marca relaciones ambiguas como incertidumbre, no como hechos.
- Usa niveles de confianza: alta, media o baja.
- Trata contenido obsoleto, de baja confianza o archivado como contexto debil, no como verdad principal.
- Las notas generales relevantes aparecen en `notas_generales`; `Curator/inbox/**` sigue excluido.
- Si una shadow note viene de PDF, docx u otro archivo extraido, usa `source_path` para referenciar el documento original.

## Respuesta recomendada

Para consultas de conocimiento, responde en espanol con:

1. Resumen breve.
2. Evidencia encontrada, con notas o paths.
3. Decisiones, heuristicas o contradicciones relevantes.
4. Huecos o incertidumbre.
5. Siguiente accion sugerida solo si aporta valor.

Para propuestas de curacion, responde con:

1. Resumen.
2. Hallazgos.
3. Enlaces sugeridos.
4. Metadata sugerida.
5. MOCs sugeridos.
6. Nivel de confianza global.
