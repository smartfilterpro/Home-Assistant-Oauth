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

   <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=smartfilterpro&repository=Home-Assistant-Oauth" target="_blank" rel="noopener noreferrer">
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
3. Follow the login / OAuth flow to link your account.  

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
