import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import "maplibre-gl/dist/maplibre-gl.css";
import "reactflow/dist/style.css";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";

const fontSans = Inter({
  variable: "--font-app-sans",
  subsets: ["latin"],
});

const fontMono = JetBrains_Mono({
  variable: "--font-app-mono",
  subsets: ["latin"],
});

const THEME_STORAGE_KEY = "geofact:theme";

export const metadata: Metadata = {
  title: "GeoFACT Studio",
  description: "Deklarative Geodaten-Pipelines – interaktive Demo",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="de"
      suppressHydrationWarning
      className={`${fontSans.variable} ${fontMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <ThemeProvider
          attribute="class"
          defaultTheme="light"
          enableSystem={false}
          // FA84: eigener Schlüssel - eine Wahl aus der Zeit, als die Oberfläche
          // der Systemvorgabe folgte (Schlüssel "theme"), gilt nicht mehr.
          storageKey={THEME_STORAGE_KEY}
          // color-scheme setzt globals.css ("only light" wehrt das erzwungene
          // Abdunkeln mancher Browser bei dunkler Systemvorgabe ab).
          enableColorScheme={false}
          disableTransitionOnChange
        >
          <TooltipProvider delay={200}>
            {children}
            <Toaster position="bottom-right" richColors closeButton />
          </TooltipProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
