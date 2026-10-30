// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import mermaid from "astro-mermaid";
import starlightThemeFlexoki from "starlight-theme-flexoki";
import lit from "@astrojs/lit";

// https://astro.build/config
export default defineConfig({
  // Old URLs that are linked from released integration versions and
  // external sites; keep them working after a page moves or is merged
  // into another one.
  redirects: {
    "/schedule": "/deep-explanations/schedule-and-night-mode",
    "/calibration": "/calibration_algorithms",
    "/qanda": "/faq/common-questions",
    "/qanda/supported": "/working-devices/compatibility",
    "/qanda/modes": "/faq/common-questions",
    "/qanda/debugging": "/faq/debugging",
    "/qanda/missing_entity": "/faq/missing-entity",
    "/qanda/degraded_mode": "/faq/degraded-mode",
    "/qanda/window_sensor": "/faq/window-sensor",
    "/hydraulic": "/deep-explanations/hydraulic-balance",
  },

  integrations: [
    mermaid({ theme: "neutral" }),
    lit(),
    starlight({
      title: "Better Thermostat",
      favicon: "/favicon.png",
      social: {
        github: "https://github.com/KartoffelToby/better_thermostat",
      },
      sidebar: [
        {
          label: "Start Here",
          items: [
            { label: "5-minute quickstart", link: "/setup/getting-started/" },
            { label: "How it works & use cases", link: "/setup/use-cases/" },
            {
              label: "Setup dialog walkthrough",
              link: "/setup/configuration-walkthrough/",
            },
            { label: "Interactive Configurator", link: "/setup/configurator/" },
            {
              label: "Recommended settings",
              link: "/optimal-settings/recommended-settings/",
            },
          ],
        },
        {
          label: "Algorithms & Settings",
          items: [
            {
              label: "Calibration types & algorithms",
              link: "/calibration_algorithms/",
            },
            {
              label: "Quick algorithm selector",
              link: "/optimal-settings/algorithm-selection/",
            },
            { label: "All settings reference", link: "/configuration/" },
            { label: "Presets", link: "/setup/presets/" },
            { label: "Entities & attributes", link: "/setup/entities/" },
          ],
        },
        {
          label: "Schedules, Boiler & Automations",
          items: [
            {
              label: "Blueprints & schedules",
              link: "/setup/automation-blueprints/",
            },
            {
              label: "Central heating & boiler control",
              link: "/deep-explanations/central-heating-thermostat/",
            },
            { label: "Pausing learning", link: "/setup/pause-learning/" },
            {
              label: "Releasing devices (manual control)",
              link: "/setup/releasing-devices/",
            },
            { label: "Automation recipes", link: "/setup/automation-recipes/" },
            {
              label: "Actions & conditions",
              link: "/setup/actions-and-conditions/",
            },
          ],
        },
        {
          label: "Hardware & Compatibility",
          items: [
            {
              label: "Supported devices & compatibility",
              link: "/working-devices/compatibility/",
            },
            {
              label: "Recommended TRVs & Zigbee",
              link: "/working-devices/recommended-devices/",
            },
          ],
        },
        {
          label: "Troubleshooting & FAQ",
          items: [
            { label: "Troubleshooting & FAQ", link: "/faq/common-questions/" },
            {
              label: "Summer mode explained",
              link: "/deep-explanations/summer-mode/",
            },
            {
              label: "Degraded mode & sensor fallback",
              link: "/faq/degraded-mode/",
            },
            { label: "Window sensor", link: "/faq/window-sensor/" },
            { label: "Door sensor", link: "/faq/door-sensor/" },
            {
              label: "Missing entity & availability guard",
              link: "/faq/missing-entity/",
            },
            {
              label: "Target temperature not taken",
              link: "/faq/setpoint-not-applied/",
            },
            {
              label: "Thermostat in multiple rooms",
              link: "/faq/shared-trv/",
            },
            {
              label: "Invalid external temperature",
              link: "/faq/invalid-external-temperature/",
            },
            { label: "Debugging & diagnostics", link: "/faq/debugging/" },
          ],
        },
        {
          label: "Upgrading & Deep Dives",
          items: [
            {
              label: "Upgrading from 1.9 to 2.0",
              link: "/setup/upgrading-to-2-0/",
            },
            {
              label: "Migrating legacy schedule actions",
              link: "/deep-explanations/schedule-and-night-mode/",
            },
            {
              label: "Virtual hydraulic balancing",
              link: "/deep-explanations/hydraulic-balance/",
            },
            {
              label: "Data updates & radio limits",
              link: "/deep-explanations/data-updates/",
            },
            {
              label: "Removing Better Thermostat",
              link: "/setup/removing/",
            },
          ],
        },
        {
          label: "Internals (Developers)",
          collapsed: true,
          autogenerate: { directory: "internals" },
        },
        {
          label: "Support",
          link: "/support/",
        },
      ],
      plugins: [starlightThemeFlexoki()],
      components: {
        Head: "./src/components/starlight/Head.astro",
      },
    }),
  ],
});
