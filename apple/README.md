# Spliff Mac companion

Open `Spliff/Spliff.xcodeproj`, choose **My Mac**, and Run. Requires macOS 14 or later and the current Xcode project format. The target is Mac-only, with bundle ID `com.sakanventures.spliff`. It is a direct-distribution companion and does not use App Sandbox because the current backend manages local Python processes, launch agents, mounted libraries, and Mac Trash.

The app embeds the existing local dashboard in WKWebView. Native controls select music folders, show service readiness, and configure the connection. The menu-bar shortcut opens the window or restarts services. Closing the window or quitting the companion leaves the installed launch agents running.

There is one main window. Settings opens inside it, with a visible close button and Escape to return to music. The embedded dashboard hides its browser-only header so the app title and folder controls are not duplicated. Updated app copies activate an already running Spliff instance instead of starting a second one.

On first launch, enter the existing tunnel runtime key into the secure field and choose **Connect**. The app uses the same service installer as the CLI, stores the key in an owner-only local file, and preserves the existing tunnel identity. Setup validates credentials/configuration before gracefully stopping the verified manual Spliff tunnel. No key is placed in command-line arguments or app resources. Readiness requires a successful control-plane poll; a loaded process alone is not considered connected. Refresh the ChatGPT connection tools after setup.

This is a development companion for the current checkout. Its default project path is supplied by the build; it can select a different checkout if unavailable. It uses the checkout's `.venv` Python, `tunnel-client`, existing tunnel profile, and installed FFmpeg. It does **not** bundle a portable runtime or provision tunnel accounts for other users yet. Public release still needs runtime packaging, a distribution signing identity/notarization, and end-user connection onboarding. Do not distribute a build containing the developer's tunnel configuration or credentials.

Build a locally ad-hoc-signed Release app with `scripts/build-mac.sh`. The app is produced under `build/macOS/Build/Products/Release/Spliff.app`. Builds and local catalog files are excluded from Git. Launch-agent installation and live network verification must run on the user's Mac outside the restricted development executor.
