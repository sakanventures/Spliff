import * as React from "react"
import { execFileSync } from "node:child_process"
import { readFileSync, writeFileSync } from "node:fs"
import { renderToStaticMarkup } from "react-dom/server"
import { Navigation, SyncWorkspace } from "./sections"

const checking = process.argv.includes("--check")
const staticDirectory = new URL("../spliff/static/", import.meta.url)

function update(file: string, sections: [string, string][]) {
  const path = new URL(file, staticDirectory)
  const source = readFileSync(path, "utf8")
  let output = source
  for (const [section, contents] of sections) {
    const markup = file.endsWith(".html")
    const start = markup ? `<!-- generated: ${section} -->` : `/* generated: ${section} */`
    const end = markup ? `<!-- /generated: ${section} -->` : `/* /generated: ${section} */`
    const opening = output.indexOf(start)
    const closing = output.indexOf(end, opening + start.length)
    if (opening === -1 || closing === -1) throw new Error(`Missing ${section} markers in ${file}`)
    const offset = opening + start.length
    output = output.slice(0, offset) + "\n" + contents + "\n" + output.slice(closing)
  }
  if (source === output) return
  if (checking) throw new Error(`${file} needs rebuilding. Run npm run build in dashboard-ui.`)
  writeFileSync(path, output)
}

const css = execFileSync("./node_modules/.bin/tailwindcss", ["-i", "theme.css", "--minify"], {
  cwd: new URL(".", import.meta.url),
  encoding: "utf8",
  stdio: ["ignore", "pipe", "inherit"],
}).trim()

update("index.html", [
  ["navigation", renderToStaticMarkup(<Navigation />)],
  ["sync workspace", renderToStaticMarkup(<SyncWorkspace />)],
])
update("app.css", [["shadcn utilities", css]])
