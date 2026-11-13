// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import mermaid from "astro-mermaid";
import starlightThemeFlexoki from "starlight-theme-flexoki";

// https://astro.build/config
export default defineConfig({
  // src/content/docs is a symlink to the repository's docs/ directory.
  // Resolving imports from the link's path lets those pages import the
  // packages installed here.
  vite: {
    resolve: { preserveSymlinks: true },
  },

  // Old URLs that are linked from released integration versions and
  // external sites; keep them working after a page moves or is merged
  // into another one.
  redirects: {
    "/schedule": "/deep-explanations/schedule-and-night-mode",
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
    starlight({
      title: "Better Thermostat",
      favicon: "/favicon.png",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/KartoffelToby/better_thermostat",
        },
      ],
      sidebar: [
        {
          label: "Setup",
          items: [{ autogenerate: { directory: "setup" } }],
        },
        {
          label: "Configuration",
          items: [
            { label: "Configuration", link: "/configuration/" },
            { label: "Calibration algorithms", link: "/calibration_algorithms/" },
          ],
        },
        {
          label: "FAQ",
          items: [{ autogenerate: { directory: "faq" } }],
        },
        {
          label: "Working devices",
          items: [{ autogenerate: { directory: "working-devices" } }],
        },
        {
          label: "Optimal settings",
          items: [{ autogenerate: { directory: "optimal-settings" } }],
        },
        {
          label: "Deep explanations",
          items: [{ autogenerate: { directory: "deep-explanations" } }],
        },
        {
          label: "Internals",
          items: [{ autogenerate: { directory: "internals" } }],
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
