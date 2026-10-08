// Implements: FA91 - loads frontend/lib/blankTemplate.ts under node (type
// stripping) and prints the template and its guard as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const { BLANK_TEMPLATE, canInsertTemplate } = await import(pathToFileURL(process.argv[2]).href);

console.log(
  JSON.stringify({
    template: BLANK_TEMPLATE,
    insert_into_empty: canInsertTemplate(""),
    insert_into_blank_lines: canInsertTemplate("  \n\n"),
    insert_into_text: canInsertTemplate("scenario:\n"),
    insert_into_template: canInsertTemplate(BLANK_TEMPLATE),
  })
);
