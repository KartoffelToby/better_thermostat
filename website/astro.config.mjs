// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightThemeFlexoki from "starlight-theme-flexoki";
import lit from "@astrojs/lit";

// https://astro.build/config
export default defineConfig({
  
  integrations: [
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
