import type { Metadata } from "next";
import "./globals.css";
import Sidebar from "@/components/Sidebar";
import TopBar from "@/components/TopBar";

export const metadata: Metadata = {
  title: "Kiln-3 Digital Twin · Calcination Combustion Control",
  description: "JK Cement — calciner combustion control digital twin console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">
        <div className="flex min-h-screen">
          <Sidebar />
          <main className="flex-1 min-w-0 p-6 scroll-thin"><TopBar />{children}</main>
        </div>
      </body>
    </html>
  );
}
