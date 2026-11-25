import * as React from "react"
import { Button } from "./components/button"
import { Card, CardContent } from "./components/card"

export function Navigation() {
  return (
    <nav className="workspace-nav" aria-label="Spliff views">
      <div className="workspace-views">
        <Button id="albums" type="button" variant="ghost" aria-pressed="true">Recommended</Button>
        <Button id="downloads" type="button" variant="ghost" aria-pressed="false">Activity</Button>
      </div>
      <div id="advanced" hidden>
        <Button id="browse" type="button" variant="ghost" aria-pressed="false">Track index</Button>
        <Button id="staged" type="button" variant="ghost" aria-pressed="false">Saved reviews</Button>
      </div>
      <Button id="sync" type="button" variant="ghost" aria-pressed="false">Settings</Button>
    </nav>
  )
}

export function SyncWorkspace() {
  return (
    <>
      <header className="sync-intro">
        <h1 id="sync-heading">Settings</h1>
        <p className="sync-description">
          Choose which folders Spliff scans for lossless upgrades.
        </p>
      </header>
      <section id="sync-folder-list" aria-labelledby="sync-folders-heading">
        <Card className="sync-library">
          <CardContent className="sync-library-head">
            <h2 id="sync-folders-heading">Music folders</h2>
            <p id="sync-folder-state" role="status">Checking folders…</p>
          </CardContent>
          <CardContent className="sync-actions">
            <Button id="sync-folder" type="button" size="lg">Choose music folders</Button>
          </CardContent>
          <ul id="sync-folders" hidden />
          <p id="sync-status" role="status" hidden />
          <details id="sync-details" hidden>
            <summary>Sync details</summary>
            <ul id="sync-items" />
          </details>
        </Card>
      </section>
    </>
  )
}
