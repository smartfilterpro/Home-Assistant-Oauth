# SmartFilterPro – Home Assistant Integration

SmartFilterPro connects your smart thermostat to [SmartFilterPro](https://smartfilterpro.com), allowing you to:

- Monitor HVAC runtime directly in Home Assistant  
- Track filter usage hours automatically  
- Get reminders when it’s time to replace your filter  
- Sync with your SmartFilterPro account for automatic filter ordering

---

## Installation

### Option A: HACS (recommended)

1. Make sure [HACS](https://hacs.xyz) is installed in your Home Assistant.  
2. Add this repository as a **custom repository** in HACS:

   [![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=smartfilterpro&repository=Home-Assistant-Oauth)

3. In Home Assistant → **HACS → Integrations**, search for **SmartFilterPro** and install it.  
4. Restart Home Assistant.

### Option B: Manual install

1. Download the latest release ZIP from [GitHub](https://github.com/smartfilterpro/Home-Assistant-Oauth/releases).  
2. Copy the folder `custom_components/smartfilterpro` into your Home Assistant `/config/custom_components/` directory.  
3. Restart Home Assistant.

---

## Configuration

After installation and restart, add the integration:

[![Add Integration to My Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=smartfilterpro)

1. Click the button above or go to **Settings → Devices & Services → Add Integration**.  
2. Search for **SmartFilterPro**.  
3. Follow the login / OAuth flow to link your account.  

---

## Requirements

- Home Assistant `2024.6.0` or newer  
- Active SmartFilterPro account  

---

## Support

- Issues: [GitHub Issues](https://github.com/smartfilterpro/Home-Assistant-Oauth/issues)  
- Docs: (coming soon)  

---

## Disclaimer

This is a custom integration and is **not yet part of Home Assistant Core**.
