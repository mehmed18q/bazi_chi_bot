# تصاویر پروفایل بازی‌چی — نسخهٔ ۲

- نسخهٔ اصلی و دست‌نخورده: [bot-logo.png](bot-logo.png)
- تصویر ربات با ابعاد درخواستی ۶۴۰×۳۶۰: [bot-logo-v2-640x360.png](bot-logo-v2-640x360.png)
- نسخهٔ مربع تصویر جدید ربات: [bot-logo-v2.png](bot-logo-v2.png)
- تصویر هماهنگ ادمین: [admin-logo-v2.png](admin-logo-v2.png)

طرح‌ها نماد گل یا پوچ، دوز، بازی‌های پرسشی و فکر بکر را با دو حباب پیام متصل ترکیب می‌کنند. حباب آبی با هواپیمای کاغذی به تلگرام اشاره دارد و حباب سبز، اشاره‌ای تصویری به بازوی بله است؛ حباب سبز لوگوی رسمی بله نیست. سپر طلایی با چرخ‌دنده، نسخهٔ ادمین را متمایز می‌کند.

روش ساخت: ابزار داخلی `image_gen`، با تصویر قبلی به‌عنوان مرجع ربات و خروجی جدید ربات به‌عنوان مرجع نسخهٔ ادمین. فایل‌های مربع خروجی مستقیم ابزار با پس‌زمینهٔ شفاف هستند. نسخهٔ افقی با همان ابزار و مرجع نسخهٔ مربع ساخته شد و با FFmpeg و فیلتر Lanczos به ابعاد دقیق ۶۴۰×۳۶۰ رسید؛ تناسب طرح و شفافیت حفظ شده و حاشیه‌های دو طرف شفاف هستند. همهٔ نسخه‌های قبلی حفظ شده‌اند.

## پرامپت ربات

```text
Use case: logo-brand.
Asset type: one square profile avatar for the Persian social games bot بازی‌چی (Bazi Chi), the BOT edition of a coordinated bot/admin pair.
Input image 1 is a reference for brand identity and visual style. Create a NEW updated illustration, not a collage of two avatars.
Primary request: evolve this cheerful purple, sunny yellow, turquoise and coral-pink game emblem to convey multiple social games AND connected messaging platforms (Telegram and Bale). Preserve the instantly recognizable central friendly closed hand with pink flower, the chunky purple outlines, rounded polished 3D cartoon shading and warm yellow circular medallion.
Composition: a beautifully balanced compact circular emblem, square canvas. Keep all important artwork safely inside the central 82% of the square so circular profile cropping cannot cut off symbols. Large central hand and flower. Behind it, one simplified tic-tac-toe tile on the left and one yellow question-mark game tile on the right, with a small row of four colorful Mastermind pegs integrated into the right tile. No need to depict all six games.
Multiplatform motif: on the lower purple curved base, place TWO equally prominent messaging badges, one sky blue circular chat badge bearing a crisp white paper airplane and one emerald green chat badge bearing a simple white speech bubble. Visually CONNECT the two with a thick smooth luminous turquoise link/arc, clearly communicating one shared gaming experience across two messaging platforms. These must be visible at small avatar scale. Between them retain a smaller warm yellow heart as the bot edition identifier.
Style: premium playful game app illustration, coherent rounded forms, clean intentional geometry, strong silhouette, crisp edges, tasteful soft highlights, very few decorative flecks. No grainy halo.
Constraints: one avatar only; no written text, no labels, no watermarks, no device mockup, no crown, no admin shield. Preserve the reference palette and flower/hand brand motif, make game and linked-message symbols clear. True transparent background outside the warm yellow circular medallion; clean alpha edges, no white square or checkerboard baked in.
```

## پرامپت ادمین

```text
Use case: precise-object-edit.
Asset type: one square ADMIN profile avatar that forms a matched pair with the attached Bazi Chi social games bot avatar.
Input image 1: the FINAL BOT AVATAR, the edit target and exact visual identity reference.
Primary request: create its matching ADMIN edition. Retain the same composition, dimensions, central friendly closed hand, pink flower and green leaves, purple tic-tac-toe tile, yellow question-mark game tile with four colored pegs, blue paper-airplane messaging badge, green white-speech-bubble messaging badge, and turquoise connection between them. These connected messaging badges must remain clear and unchanged, expressing Telegram and Bale.
Make ONE focal change: replace the small yellow heart on the bottom-center purple base with a larger, prominent GOLDEN ADMIN SHIELD bearing a crisp deep-purple GEAR symbol at its center. Make this shield approximately twice the area of the original heart, visually prominent at avatar scale. Give it a clear simple silhouette, a rich golden beveled edge and a solid sunny gold interior with one bold purple cog; do not put a heart on the shield. Integrate it naturally in front of the center of the existing purple base, slightly overlapping upward into the hand's lower wrist if needed, without covering either messaging badge or the game symbols. This is the unmistakable management identifier.
Preserve all other elements, exact colors, warm yellow circular medallion, purple outlines, polished rounded 3D cartoon rendering, cheerful mood, scale, perspective and placement of the reference. Keep image as close to the original as possible apart from the shield change. One avatar only, no side-by-side comparison. No written text, no crown, no extra symbols, no watermark. Preserve true transparency outside the medallion and clean alpha edges.
```

## پرامپت نسخهٔ ۶۴۰×۳۶۰ ربات

```text
Use case: precise-object-edit.
Asset type: landscape export of the existing Bazi Chi BOT image, exactly 640 pixels wide by 360 pixels high (16:9 aspect ratio).
Input image 1 is the existing finished bot artwork. Preserve the entire supplied artwork and every symbol, color, proportion and detail as faithfully as possible: central hand and pink flower, purple tic-tac-toe tile, yellow question-mark tile with colored pegs, linked blue paper-airplane and green speech-bubble badges, yellow heart, yellow circular medallion.
Change ONLY the canvas format: place the complete artwork centered on a landscape 16:9 transparent canvas. Fit the full circular emblem into the canvas height, with a small safe margin above and below. Leave equal transparent margins on both sides. Scale proportionally; never stretch or crop any part. No new illustration, no rearranged elements, no text, no decoration, no shield, no background fill. Preserve true transparency.
Requested final image dimensions: 640 × 360 pixels, landscape.
```


