# SmartFilterPro – Home Assistant Integration

SmartFilterPro connects your smart thermostat to 
<a href="https://smartfilterpro.com" target="_blank" rel="noopener noreferrer">SmartFilterPro</a>, allowing you to:

- Monitor HVAC runtime directly in Home Assistant  
- Track filter usage hours automatically  
- Get reminders when it’s time to replace your filter  
- Sync with your SmartFilterPro account for automatic filter ordering

---

## Installation

### Option A: HACS (recommended)

1. Make sure <a href="https://hacs.xyz" target="_blank" rel="noopener noreferrer">HACS</a> is installed in your Home Assistant.  
2. Add this repository as a **custom repository** in HACS:

   <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=smartfilterpro&repository=Home-Assistant-Oauth&category=integration" target="_blank" rel="noopener noreferrer">
     <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Add to HACS">
   </a>

3. In Home Assistant → **HACS → Integrations**, search for **SmartFilterPro** and install it.  
4. Restart Home Assistant.

### Option B: Manual install

1. Download the latest release ZIP from <a href="https://github.com/smartfilterpro/Home-Assistant-Oauth/releases" target="_blank" rel="noopener noreferrer">GitHub</a>.  
2. Copy the folder `custom_components/smartfilterpro` into your Home Assistant `/config/custom_components/` directory.  
3. Restart Home Assistant.

---

## Configuration

After installation and restart, add the integration:

<a href="https://my.home-assistant.io/redirect/config_flow_start?domain=smartfilterpro" target="_blank" rel="noopener noreferrer">
  <img src="https://my.home-assistant.io/badges/config_flow_start.svg" alt="Add Integration to My Home Assistant">
</a>

1. Click the button above or go to **Settings → Devices & Services → Add Integration**.  
2. Search for **SmartFilterPro**.  
3. If asked where to sign in, choose **SmartFilterPro** (the development option is for testers).  
4. A SmartFilterPro page opens. Sign in the way you do in the SmartFilterPro app (**Apple, Google or email**), then choose **Allow**. Home Assistant picks up from there.  
5. If your account has several Home Assistant thermostats, choose the one to link, then (optionally) the Home Assistant climate entity to forward.  

Home Assistant never sees your password. The integration gets its own sign-in from SmartFilterPro.

**"No address SmartFilterPro can send you back to":** SmartFilterPro returns you to Home Assistant through **My Home Assistant** (`https://my.home-assistant.io/redirect/oauth`). It's part of Home Assistant's default configuration; if you removed `default_config:` or `my:` from `configuration.yaml`, add `my:` back and restart.

**Upgrading from 0.2.x:** entries you already set up with email and password keep working; nothing to do. The email and password form is gone, so new entries use Sign in with SmartFilterPro.

---

## Choosing a version

HACS only offers **published GitHub Releases** of this repository, never raw branches.

- **Stable releases** (`v0.1.0`, `v0.2.0`, …) are what every HACS user sees and gets on update.
- **Beta releases** (`v0.2.0-beta.1`, …) are marked *pre-release*. HACS hides them unless you open the SmartFilterPro entry in HACS, choose **Redownload**, and turn on **Show beta versions**. Use these only when SmartFilterPro support asks you to test a fix.
- To go back to an earlier version, use **Redownload** and pick it from the list.

The version shown in Home Assistant (Settings → Devices & Services → SmartFilterPro) always matches the release tag.

## Environment (for testers)

When you add the integration, Home Assistant asks where to sign in: **SmartFilterPro** (production) or **SmartFilterPro (dev / test)**. Choose production unless SmartFilterPro support asked you to test against the development environment. The choice sets both the SmartFilterPro app you sign in to and the server the integration reports runtime to, so the two always match.

---

## Requirements

- Home Assistant `2024.6.0` or newer  
- Active SmartFilterPro account  

---

## Support

- Issues: <a href="https://github.com/smartfilterpro/Home-Assistant-Oauth/issues" target="_blank" rel="noopener noreferrer">GitHub Issues</a>  
- Docs: (coming soon)  

---

## Disclaimer

This is a custom integration and is **not yet part of Home Assistant Core**.

---

## For maintainers: branches and releases

- `dev` is where changes land first. `main` only receives merges from `dev` once they have been tested.
- Merging to `main` publishes nothing. Users get a version only when a **GitHub Release** is published, so a release is a deliberate step.
- To release: bump `"version"` in `custom_components/smartfilterpro/manifest.json` on `dev`, merge to `main`, then on GitHub choose **Releases → Draft a new release**, create tag `v<that version>` on `main`, and publish. Tick **pre-release** only for a `-beta.N` version.
- The *Release* workflow then checks that the tag and the manifest version agree, that pre-release is ticked for betas and not otherwise, and attaches `smartfilterpro.zip` for manual installs. If the check fails, fix the manifest and re-tag; HACS users never see a mismatched version.
- The *Validate* workflow runs Home Assistant's `hassfest` and the HACS validator on every push and pull request.
