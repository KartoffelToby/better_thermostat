// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import mermaid from "astro-mermaid";
import starlightThemeFlexoki from "starlight-theme-flexoki";
import lit from "@astrojs/lit";

// https://astro.build/config
export default defineConfig({
  // Old URLs that are linked from released integration versions and
  // external sites; keep them working after the Q&A → FAQ migration.
  redirects: {
    "/qanda": "/faq/common-questions",
    "/qanda/supported": "/working-devices/compatibility",
    "/qanda/modes": "/faq/common-questions",
    "/qanda/debugging": "/faq/debugging",
    "/qanda/missing_entity": "/faq/missing-entity",
    "/qanda/degraded_mode": "/faq/degraded-mode",
    "/qanda/window_sensor": "/faq/window-sensor",
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
          label: "Setup",
          autogenerate: { directory: "setup" },
        },
        {
          label: "FAQ",
          autogenerate: { directory: "faq" },
        },
        {
          label: "Working devices",
          autogenerate: { directory: "working-devices" },
        },
        {
          label: "Optimal settings",
          autogenerate: { directory: "optimal-settings" },
        },
        {
          label: "Deep explanations",
          autogenerate: { directory: "deep-explanations" },
        },
        {
          label: "Internals",
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
