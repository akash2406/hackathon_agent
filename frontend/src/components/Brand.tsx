/**
 * Brand mark shown on every page (sidebar, sign-in, mobile header).
 *
 * Drop the official logo file from your brand portal at
 *   frontend/public/brand/logo.svg
 * and it is used everywhere automatically (rebuild the image). Until then a
 * plain text mark is shown. We deliberately do not ship a hand-drawn copy of a
 * company logo; use the official asset and follow its brand guidelines.
 */
import { useState } from "react";

const BRAND_NAME = "Telstra";

export function BrandLogo({ size = 28, variant = "dark" }: { size?: number; variant?: "dark" | "light" }) {
  const [failed, setFailed] = useState(false);
  if (!failed) {
    return (
      <img
        className={`brand-logo ${variant}`}
        src="/brand/logo.svg"
        alt={BRAND_NAME}
        height={size}
        onError={() => setFailed(true)}
      />
    );
  }
  return (
    <span className={`brand-text ${variant}`} style={{ fontSize: size * 0.72 }} aria-label={BRAND_NAME}>
      {BRAND_NAME}
    </span>
  );
}

export function ProductMark() {
  return (
    <span className="product-mark">
      <span className="product-name">CRIP</span>
      <span className="product-tag">FinOps copilot</span>
    </span>
  );
}
