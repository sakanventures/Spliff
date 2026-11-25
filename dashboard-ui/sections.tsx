import * as React from "react"
import { Button } from "./components/button"
import { Card, CardContent } from "./components/card"

export function Navigation() {
  return (
    <nav className="workspace-nav" aria-label="Spliff views">
      <div className="workspace-views">
        <Button id="sync" type="button" variant="ghost" aria-pressed="true">
          Sync
        </Button>
        <Button id="downloads" type="button" variant="ghost" aria-pressed="false">
          Downloads
        </Button>
      </div>
      <details id="advanced">
        <summary>Advanced</summary>
        <div className="advanced-menu">
          <Button id="albums" type="button" variant="ghost" aria-pressed="false">Find albums manually</Button>
          <Button id="browse" type="button" variant="ghost" aria-pressed="false">Track index</Button>
          <Button id="staged" type="button" variant="ghost" aria-pressed="false">Staged reviews</Button>
          <Button id="open-settings" type="button" variant="ghost">Settings</Button>
        </div>
      </details>
    </nav>
  )
}

export function SyncWorkspace() {
  return (
    <>
      <header className="sync-intro">
        <h1 id="sync-heading">Upgrade your music.</h1>
        <p className="sync-description">
          Choose which folders Spliff scans for lossless upgrades.
          Review each download before deciding what to keep.
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
            <Button id="sync-downloads" type="button" size="lg" variant="outline">View downloads</Button>
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
