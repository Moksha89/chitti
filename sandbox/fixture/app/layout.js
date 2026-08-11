import "./globals.css";

export const metadata = {
  title: "Chitti Motion Lab",
  description: "Deterministic React Three Fiber fixture",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
