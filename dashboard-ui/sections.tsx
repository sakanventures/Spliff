import * as React from "react"
import { Button } from "./components/button"
import { Card, CardContent, CardDescription } from "./components/card"

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

function RecordIllustration() {
  return (
    <div className="record-illustration" aria-hidden="true">
      <div className="record-artwork">
        <div className="vinyl-disc" />
        <div className="record-sleeve"><span className="spliff-mark" /></div>
      </div>
    </div>
  )
}

export function SyncWorkspace() {
  return (
    <>
      <Card className="sync-hero">
        <CardContent className="sync-intro">
          <h1 id="sync-heading">Upgrade your music.</h1>
          <CardDescription className="sync-description">
            Choose which folders Spliff scans for lossless upgrades.
            Review each download before deciding what to keep.
          </CardDescription>
          <div className="sync-actions">
            <Button id="sync-folder" type="button" size="lg">Choose music folders</Button>
            <Button id="sync-downloads" type="button" size="lg" variant="outline">View downloads</Button>
          </div>
        </CardContent>
        <RecordIllustration />
      </Card>
      <section id="sync-folder-list" aria-labelledby="sync-folders-heading">
        <Card className="sync-library">
          <CardContent className="sync-library-head">
            <h2 id="sync-folders-heading">Music folders</h2>
            <p id="sync-folder-state" role="status">Checking folders…</p>
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
