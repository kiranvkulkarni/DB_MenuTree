"""Settings sheet of the S25 Ultra MenuTree, transcribed from the photographs.

Source: S25_Ultra_MenuTree/Settings.pdf, 4 pages, photographs of Excel showing
SRIB-SE_PA3_S938U_Menu_Tree_Test_Results_AXKK_MQB88781393_1B_NOV_2024.xlsx

Transcribed by reading the page images rather than by OCR. Tesseract was
measured first and rejected: it silently dropped whole columns (depth 2 and
depth 6 came back empty on page 1 although the screenshot plainly shows
"Quick settings", "Default", "Roboto", "Noto Serif"), and a missing row in a
reference spec is never verified and never flagged.

COMPLETENESS CHECK: the sheet's own summary block reads Total 88 / Pass 88.
The transcription is rows 5..92 inclusive = 88 rows. They agree.

Column mapping read off the header row: E=1 Depth, F=2, G=3, H=4, I=5, J=6.
Consecutive pages overlap, which was used to cross-check readings -- row 75
reads "Selfire angle" on two independent pages, so that typo is the sheet's,
not a misreading. Likewise row 35 appears clipped as "RAW and JPEG" on page 1
and in full as "RAW and JPEG formats" on page 2.

KNOWN LIMITATION: cells clipped by column width are clipped in the source
photograph too. Row 5 displays "Camer"; the app is "Camera", recorded here as
the obvious expansion and marked below. Any other clipped cell that the page
overlaps did not recover is a genuine unknown.
"""

# (excel_row, depth, label)
SETTINGS = [
    (5,  1, "Camera"),        # displayed clipped as "Camer"
    (6,  2, "Quick settings"),
    (7,  3, "Go to settings"),
    (8,  4, "Scan documents and text"),
    (9,  5, "Auto scan"),
    (10, 5, "Remove unwanted"),
    (11, 4, "Scan QR codes"),
    (12, 4, "Shot Suggestions toggle button"),
    (13, 4, "Intelligent optimization"),
    (14, 5, "Maximum"),
    (15, 5, "medium"),
    (16, 5, "minimum"),
    (17, 5, "Scene optimizer - toggle button"),
    (18, 4, "Swipe Shutter button to"),
    (19, 5, "Take burst shot"),
    (20, 5, "Create GIF"),
    (21, 4, "Watermark"),
    (22, 5, "Watermark - toggle button"),
    (23, 5, "Custom"),
    (24, 5, "Date"),
    (25, 5, "Time"),
    (26, 5, "Font"),
    (27, 6, "Default"),
    (28, 6, "Roboto"),
    (29, 6, "Noto Serif"),
    (30, 5, "Alignment"),
    (31, 4, "Advanced picture options"),
    (32, 5, "High efficiency pictures - toggle button"),
    (33, 5, "Pro mode picture format"),
    (34, 6, "JPEG format"),
    (35, 6, "RAW and JPEG formats"),
    (36, 6, "RAW format"),
    (37, 5, "Motion photo capture"),
    (38, 6, "Before and after shutter"),
    (39, 6, "Before shutter only"),
    (40, 4, "Save selfies as previewed - toggle button"),
    (41, 4, "Swipe up/down to switch cameras"),
    (42, 4, "Auto FPS"),
    (43, 5, "Off"),
    (44, 5, "Use for 30fps videos only"),
    (45, 5, "Use for 30 fps and 60 fps videos"),
    (46, 4, "Video Stabilization - toggle button"),
    (47, 4, "Advanced video options"),
    (48, 5, "HEVC (high efficiency)"),
    (49, 5, "H.264 (most compatible)"),
    (50, 5, "High biterate videos- toggle button"),
    (51, 5, "HDR- toggle button"),
    (52, 5, "Log- toggle button"),
    (53, 5, "Zoom-in-mic- toggle button"),
    (54, 5, "360 audio recording- toggle button"),
    (55, 5, "Audio playback- toggle button"),
    (56, 4, "3D capture- toggle button"),
    (57, 4, "Tracking auto focus- toggle button"),
    (58, 4, "Grid lines - toggle button"),
    (59, 4, "Location tags - toggle button"),
    (60, 5, "Turn on improve location accuracy?........"),
    (61, 5, "Not now"),
    (62, 5, "Turn on"),
    (63, 6, "While using the app"),
    (64, 6, "Only this time"),
    (65, 6, "Don't allow"),
    (66, 4, "Shooting methods"),
    (67, 5, "Press Volume buttons to"),
    (68, 6, "Zoom in or out"),
    (69, 6, "Control sound volume"),
    (70, 5, "Voice commands- toggle button"),
    (71, 5, "Floating Shutter button - toggle button"),
    (72, 5, "Show palm - toggle"),
    (73, 4, "Settings to keep"),
    (74, 5, "Camera mode"),
    (75, 5, "Selfire angle"),        # sheet's own typo, confirmed on two pages
    (76, 5, "High picture resolutions"),
    (77, 5, "Filters"),
    (78, 5, "Super Steady"),
    (79, 5, "Potrait zoom"),         # sheet's own typo for "Portrait"
    (80, 5, "Exposure"),
    (81, 4, "Shutter sound"),
    (82, 4, "Vibration feedback"),
    (83, 4, "Permissions"),
    (84, 5, "Required permissions... Other permissions..."),
    (85, 4, "Reset settings"),
    (86, 5, "Reset camera settings?"),
    (87, 5, "Cancel"),
    (88, 5, "Reset"),
    (89, 4, "About Camera"),
    (90, 5, "Info icon"),
    (91, 5, "Camera Version....."),
    (92, 5, "Open source licenses"),
]

EXPECTED_TOTAL = 88   # from the sheet's own summary block
