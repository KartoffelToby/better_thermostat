// @ts-check
import { existsSync, readFileSync } from "node:fs";
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import starlight from "@astrojs/starlight";
import mermaid from "astro-mermaid";
import starlightThemeFlexoki from "starlight-theme-flexoki";
import starlightLinksValidator from "starlight-links-validator";
import { sidebar } from "./src/sidebar.mjs";

// https://astro.build/config
export default defineConfig({
  site: "https://better-thermostat.org",

  // src/content/docs is a symlink to the repository's docs/ directory.
  // Resolving imports from the link's path lets those pages import the
  // packages installed here.
  vite: {
    resolve: { preserveSymlinks: true },
  },

  // Old URLs that are linked from released integration versions and
  // external sites; keep them working after a page moves or is merged
  // into another one. The targets end in a slash like the pages' canonical
  // URLs, so a redirect lands on the page without a second hop.
  redirects: {
    "/schedule": "/deep-explanations/schedule-and-night-mode/",
    "/qanda": "/faq/common-questions/",
    "/qanda/supported": "/working-devices/compatibility/",
    "/qanda/modes": "/faq/common-questions/",
    "/qanda/debugging": "/faq/debugging/",
    "/qanda/missing_entity": "/faq/missing-entity/",
    "/qanda/degraded_mode": "/faq/degraded-mode/",
    "/qanda/window_sensor": "/faq/window-sensor/",
    "/hydraulic": "/deep-explanations/hydraulic-balance/",
  },

  integrations: [
    // Starlight adds the sitemap on its own, but without dates. Listing it
    // here replaces that one. The sitemap is written after the pages, so
    // each <lastmod> is read from the date src/routeData.ts put in the page.
    sitemap({
      serialize(item) {
        const page = `dist${new URL(item.url).pathname}index.html`;
        if (!existsSync(page)) return item;
        const lastmod =
          /property="article:modified_time" content="([^"]+)"/.exec(
            readFileSync(page, "utf-8"),
          )?.[1];
        return lastmod ? { ...item, lastmod } : item;
      },
    }),
    mermaid({ theme: "neutral" }),
    starlight({
      title: "Better Thermostat",
      favicon: "/favicon.png",
      head: [
        {
          tag: "meta",
          attrs: {
            property: "og:image",
            content: "https://better-thermostat.org/og.png",
          },
        },
        {
          tag: "meta",
          attrs: { property: "og:image:width", content: "1200" },
        },
        {
          tag: "meta",
          attrs: { property: "og:image:height", content: "630" },
        },
        {
          tag: "meta",
          attrs: {
            property: "og:image:alt",
            content:
              "Better Thermostat logo: Smart TRV control for Home Assistant",
          },
        },
        // Lets search results show the full-size preview image.
        {
          tag: "meta",
          attrs: { name: "robots", content: "max-image-preview:large" },
        },
        {
          tag: "link",
          attrs: { rel: "apple-touch-icon", href: "/apple-touch-icon.png" },
        },
      ],
      // Pull requests target develop, so edits start there.
      editLink: {
        baseUrl:
          "https://github.com/KartoffelToby/better_thermostat/edit/develop/",
      },
      routeMiddleware: "./src/routeData.ts",
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/KartoffelToby/better_thermostat",
        },
      ],
      sidebar,
      plugins: [starlightThemeFlexoki(), starlightLinksValidator()],
      components: {
        Head: "./src/components/starlight/Head.astro",
      },
    }),
  ],
});
