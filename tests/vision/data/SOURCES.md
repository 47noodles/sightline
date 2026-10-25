# Test data sources

Photos used to check which eye MediaPipe calls left and right, and to smoke
test the real Face Landmarker.  All come from Wikimedia Commons.  Each was
cropped around the face and scaled down (longest side at most 640 px); no
other changes.  "Closed eye" is stated from the subject's own point of view;
in these unmirrored photos the subject's left eye appears on the image's
right.

| File | Shows | Source | Author | Licence |
|---|---|---|---|---|
| `open_glover.jpg` | both eyes open, facing the camera | [Victor J. Glover official portrait](https://commons.wikimedia.org/wiki/File:Victor_J._Glover_official_portrait.jpg) (NASA, [Flickr](https://www.flickr.com/photos/nasa2explore/12774199183/)) | Robert Markowitz / NASA | Public domain (work of the US federal government) |
| `closed_dreis.jpg` | both eyes closed | [Haley Dreis @ NAMM Show 01 27 2024](https://commons.wikimedia.org/wiki/File:Haley_Dreis_@_NAMM_Show_01_27_2024_(53835061298).jpg) | Justin Higuchi | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| `wink_left_bs70.jpg` | subject's left eye closed | [Beauty girl](https://commons.wikimedia.org/wiki/File:Beauty_girl.jpg) | bs70 (Flickr) | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| `wink_right_drost.jpg` | subject's right eye closed, head turned to their left | [Stipe Miocic (cropped)](https://commons.wikimedia.org/wiki/File:Stipe_Miocic_(48086547846)_(cropped).jpg) | Erik Drost | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |
| `wink_right_qz10.jpg` | subject's right eye closed, close up | [Wink.JPG](https://commons.wikimedia.org/wiki/File:Wink.JPG) | Qz10 | Public domain |
| `wink_right_simoes.jpg` | subject's right eye closed, head tilted | [Squint+wink+grin](https://commons.wikimedia.org/wiki/File:Squint%2Bwink%2Bgrin.jpg) | Pedro Ribeiro Simões | [CC BY 2.0](https://creativecommons.org/licenses/by/2.0) |

`face_template.npz` holds the MediaPipe landmarks, blendshapes and
transformation matrix measured on `open_glover.jpg`.  The synthetic face
generator in `tests/vision/vision_synth.py` starts from it.

What the photos showed (Face Landmarker v1, float16): `eyeBlinkLeft` rises
when the eye around landmarks 263/362 closes, which is the subject's own left
eye; flipping a photo horizontally swaps the two.  A closed eye in a smiling
wink scored only 0.22 to 0.55 on its blendshape, which is why closure also
uses the eye aspect ratio.
