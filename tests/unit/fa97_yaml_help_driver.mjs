// Implements: FA97 - loads frontend/lib/yamlHelp.ts under node (type stripping)
// and prints its data as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const help = await import(pathToFileURL(process.argv[2]).href);

console.log(
  JSON.stringify({
    sections: help.YAML_HELP_SECTIONS.map((s) => ({
      id: s.id,
      title: s.title,
      text: s.text,
      bullets: s.bullets ?? null,
      yaml: s.yaml ?? null,
      mentions: s.mentions ?? null,
    })),
    example: help.YAML_HELP_EXAMPLE,
    parts: [...help.YAML_HELP_EXAMPLE_PARTS],
    pointers: help.YAML_HELP_POINTERS,
    api_docs_url: help.apiDocsUrl("http://127.0.0.1:8000/"),
    api_docs_url_bare: help.apiDocsUrl("https://host"),
  })
);
