# Cool Stuff People Have Done with LTX 2

A community-sourced roundup of the most impressive, creative, and technically interesting things the Banodoco community has pulled off with LTX Video 2.x (primarily LTX 2.3).

---

## 1. IC-LoRAs: Extending the Model to State-of-the-Art at Specific Tasks

LTX's IC-LoRA (Image-Conditioned LoRA) architecture has become one of the most powerful extension mechanisms in open-source video generation. The community hasn't just used IC-LoRAs for style -- they've invented entirely new capabilities for the base model.

### Outpainting -- oumoumad
**oumoumad** trained an Outpaint IC-LoRA that extends the canvas of any input video by generating new content in regions marked as pure black. Sides, top, bottom, any combination -- the model fills it in with temporally consistent content. Trained on 111 videos with 324 references (same videos with random crops masked black). 16 reactions -- the top-reacted generation the day it dropped.

oumoumad explained the key insight behind training: "working in reverse is easier. Take normal videos as targets, randomly crop/mask them to create references." He also noted IC-LoRAs are easier to train than many think -- "like training an image edit model: instead of image/caption pairs, you use video/video before/after pairs, and the training process is the same as regular LoRAs." He never needed to go beyond 5000 steps, and often saw the desired effect by ~1500.

- [Outpaint comparison: input vs output](https://cdn.discordapp.com/attachments/1491836432040464394/1491836434695323658/comparison_input_vs_output.mp4?ex=69d92473&is=69d7d2f3&hm=c33d326751b1beb39324dd6ad315ffcca329e1417317e019ca7a215b04910433&) (16 reactions)
- [Outpaint gamma roundtrip showcase](https://cdn.discordapp.com/attachments/1491836432040464394/1492616461418496210/showcase_gamma_roundtrip.mp4?ex=69dbfae7&is=69daa967&hm=95590fdaae173457592faa32d303b973c484f335a3aaa8364fb95886a52f7a32&)
- Model: [huggingface.co/oumoumad/LTX-2.3-22b-IC-LoRA-Outpaint](https://huggingface.co/oumoumad/LTX-2.3-22b-IC-LoRA-Outpaint)

### Camera Motion Transfer -- Cseti
**Cseti** released an IC-LoRA that replicates camera movements from reference videos in new scenes. Trained on 77 video pairs from Pexels over 20-24 hours. It opens up precise camera control that LTX typically resists. Cseti noted limitations with more dynamic movements and cases where the reference becomes "too strong," sometimes just reproducing the reference video rather than just its camera motion.

- [Camera motion transfer: demo gif from HuggingFace](https://huggingface.co/Cseti/LTX2.3-22B_IC-LoRA-Cameraman_v1/resolve/main/test3_00029.gif)
- Model: [huggingface.co/Cseti/LTX2.3-22B_IC-LoRA-Cameraman_v1](https://huggingface.co/Cseti/LTX2.3-22B_IC-LoRA-Cameraman_v1)

### Face/Head Swapping -- Alisson Pereira
**Alisson Pereira** built BFS (Best Face Swap), described as the first open-source head-swap solution for video. Pom called it "probably state of the art for this with just a few weeks worth of compute" (27 reactions on that post). Multiple versions evolved from face-only to full head swap, with Klein integration for image-level swaps. Alisson defended a maskless approach as being less likely to break than masked alternatives, and spent extensive time building paired datasets for training.

- [Face swap side-by-side comparison](https://cdn.discordapp.com/attachments/1138790534987661363/1484760942498545725/side_by_side_sequence.mp4?ex=69bf66e2&is=69be1562&hm=4d29d215b6e632a792821e2eb16cfd83f491ec2f3be44b631db065e835937b83&) (27 reactions)
- Models: [huggingface.co/Alissonerdx/BFS-Best-Face-Swap](https://huggingface.co/Alissonerdx/BFS-Best-Face-Swap)

### Video Detailing -- Ablejones
**Ablejones** pushed the official LTX detailer IC-LoRA to its limits, calling it "pretty magical." The breakthrough: using the original video as a guide eliminates subtle detail drift between context windows -- "There's no longer any subtle drifting of details between context windows" (4 reactions).

- [Detailer IC-LoRA result](https://cdn.discordapp.com/attachments/1458032975982755861/1492267842282852543/LTX2_Stage2_00009-audio.mp4?ex=69dab63a&is=69d964ba&hm=9f10184fbc6827e263846488502dda6645d089c0f9fd4e70acc0576dcdae8dae&) (2 reactions)
- [Detailer with original video as guide -- no drift](https://cdn.discordapp.com/attachments/1458032975982755861/1491435397702619268/LTX2_Stage2_00014-audio.mp4?ex=69d7aef4&is=69d65d74&hm=29fbe5464ca0ab76372f921cf4eb501cb7b61bb0a5ce1e5b181745d90fe6d211&) (4 reactions)

### Depth Control Before Official ControlNets -- oumoumad
**oumoumad** trained his own depth IC-LoRA for car animation *before* Lightricks released any official controlnets, and had it working at just 1000 steps. As he later explained: "The controlnets (depth, pose, canny) in LTX are all IC LoRAs." This insight enabled the entire community IC-LoRA ecosystem.

- [oumoumad's depth IC-LoRA car animation (Jan 2026)](https://cdn.discordapp.com/attachments/1309520535012638740/1491859259301040280/2026-01-05_21-24-54.mkv?ex=69d939b4&is=69d7e834&hm=b289d9e8967bdd34873ee08b29655612fe31ec33dd8f56c999853054eb30fdbb&)

### Scene Transitions -- siraxe
**siraxe** released MergeGreen IC-LoRA for smooth transitions between start and end frames using green-frame markers. He also developed a TTM (text-to-motion) IC-LoRA at [huggingface.co/siraxe/TTM_IC-lora_ltx2.3](https://huggingface.co/siraxe/TTM_IC-lora_ltx2.3).

- [MergeGreen scene transition demo](https://cdn.discordapp.com/attachments/1458032975982755861/1491281153032978572/AnimateDiff_00013-audio_1.mp4?ex=69d71f4d&is=69d5cdcd&hm=f0a19c6107ddf925f5a15f7ee3bb5789dbf6f74c2345a17e2268d01c928e5299&)

### Post-Production Focus Changes -- oumoumad
**oumoumad** previewed ReFocus IC-LoRA enabling post-production depth-of-field changes without reshooting -- a capability that doesn't exist in any other open model. "Shot a video only to find the wrong subject in focus? Usually, that's a mandatory re-shoot, but LTX IC LoRA is blurring (pun unintended) the lines" (7 reactions).

- [ReFocus IC-LoRA: stacked before/after focus change](https://cdn.discordapp.com/attachments/1309520535012638740/1491853959646810192/stacked_output_reFocus.mp4) (7 reactions)

### Masked Reference-to-Video Inpainting -- Alisson Pereira
**Alisson Pereira** released MR2V (Masked Reference-to-Video), allowing users to place reference objects into videos using masks with a specific green/magenta template. He found rank 64 was too tied to the mask format, so he reduced to rank 32 to make it more general-purpose (7 reactions on release).

- [MR2V comparison reel: 4 side-by-side reference inpainting demos](https://cdn.discordapp.com/attachments/1225995744549408829/1491260895500832939/alisson_mr2v_combined_small.mp4?ex=69d70c6f&is=69d5baef&hm=4fd8f719eae4719fae44eebfc9041c597195aa55afd6d77737f96d0b51a78c0d&)
- [MR2V comparison: reference-based inpainting result](https://cdn.discordapp.com/attachments/1491391761241604126/1491391762231591014/comparison_01565-audio.mp4?ex=69d78650&is=69d634d0&hm=436e55107b8f81a9654f374b463b0cb1fae10d7d463cab324edc67d63685db5e&) (7 reactions)

### Style Transfer via IC-LoRA -- oumoumad
**oumoumad** demonstrated using IC-LoRA for style transfer by feeding source video frames directly into the guide node without preprocessing, combining IC-LoRA with regular style LoRAs. A game-changing v2v technique that's simpler than anyone expected.

### Cinema4D Depth Maps for Camera Control -- N0NSens
**N0NSens** demonstrated IC-LoRA Union Control with Cinema4D depth maps (rendered via Cinema4D playblast > AIO Aux Preprocessor > depth map), achieving precise camera movements that LTX typically resists. Combined IC-LoRA with FLF (first/last frame) using LTXVImgToVideoInplaceKJ (5 reactions).

- [N0NSens Cinema4D depth-driven camera control](https://cdn.discordapp.com/attachments/1309520535012638740/1490704191243419748/Comp_00001-audio.mp4?ex=69d505f7&is=69d3b477&hm=96b0c320a7030d9d8adb856f8af7206a8505bfec4c89779635768563ad91b2f5&) (5 reactions)

---

## 2. Style LoRAs: Bending Aesthetics to Production Quality

The community has trained LTX to faithfully reproduce a strikingly wide range of visual styles, often to a degree that surprised even experienced practitioners.

### Pixar/CGI Toon -- VRGameDevGirl84
**VRGameDevGirl84** released a Pixar-style LoRA for LTX 2.3 with step-by-step XYZ comparison showing quality at different training stages. More detailed prompts with trigger words like "pixar style character", "pixar-style animation", and "stylized CGI character" produce better results. **el marzocco** confirmed adding just "Pixar like" to a basic prompt activated the style.

- [Pixar LoRA XYZ comparison across training steps](https://cdn.discordapp.com/attachments/1457981700817817620/1492644132173385888/Pixar_XYZ_COMPARE_step9000.mp4?ex=69dc14ad&is=69dac32d&hm=56290a01ff9cc04533e1128fd9c58e95ea997a87f63a07fd86de6610164af368&)
- [Pixar LoRA example generation](https://cdn.discordapp.com/attachments/1457981700817817620/1492697876537806878/video_00033-audio.mp4?ex=69dc46ba&is=69daf53a&hm=3779ee3bb528b0d736236c245328f10ade5cc7964732199706092c7025e8772a&) (2 reactions)
- Model: [civitai.com/models/2536130](https://civitai.com/models/2536130)

### Golden Age Comics -- VRGameDevGirl84
**VRGameDevGirl84** trained a Golden Age Comic LoRA overnight and released it to 7 reactions -- the top generation of its day. The retro comic aesthetic transferred cleanly to video with audio.

- [Golden Age Comic LoRA generation](https://cdn.discordapp.com/attachments/1458032975982755861/1492171431084294164/video_00005-audio.mp4?ex=69da5c70&is=69d90af0&hm=2962171481e83561fcce5a9083a35873f6615ea71fd298206f8ddda30556f71d&) (7 reactions)
- Model: [civitai.com/models/2532516/ltx-23-golden-age-comic](https://civitai.com/models/2532516/ltx-23-golden-age-comic)

### Anime -- crinklypaper & Fill
**crinklypaper's** anime LoRA (Gurren Lagann style) reached 34,000 steps after 70+ hours of training. Key finding: LTX picks up style quickly but needs extended training for fine character details like pupil sizes ("Not perfect, but it knows simon's pupil should be smaller, yoko's bigger"). **U-ra-be-we** was showing off crinklypaper's LoRAs in other communities, earning them new fans (5 reactions).

Meanwhile, **Fill** confirmed work on a full anime finetune with 50k captioned anime clips ready for training -- a community-scale effort.

- [crinklypaper anime LoRA at 45k steps](https://cdn.discordapp.com/attachments/1457981700817817620/1492853536936169603/LTX_2.3_t2v_01131_.mp4?ex=69dcd7b3&is=69db8633&hm=72b3710d6990a879b539c71065b534259f438c4e7cc25a473cbc91e9b8522ce2&) (3 reactions)
- [crinklypaper Gurren Lagann LoRA release](https://cdn.discordapp.com/attachments/1457981700817817620/1492889133952274472/LTX_2.3_t2v_01095_.mp4?ex=69dcf8da&is=69dba75a&hm=9a55e9988816279893a16ed4dbe3c3268e250ce09f8e90759fb80d7d5093ba40&)
- [U-ra-be-we showcasing VRGameDevGirl's LoRAs](https://cdn.discordapp.com/attachments/1458032975982755861/1492511873248002108/LTX-2_00286_.mp4?ex=69db9980&is=69da4800&hm=f289d66f045825d0945e14be3e5a6e901cbbf814946ed3b43cd5e4666f8d6dbd&) (5 reactions)
- Model: [civitai.com/models/2537530/anime-style-gurren-lagann-lora-ltx-23](https://civitai.com/models/2537530/anime-style-gurren-lagann-lora-ltx-23)

### Anime-to-Realism -- Alisson Pereira
**Alisson Pereira's** anime2half-realism IC-LoRA was designed to convert anime footage to photorealism, but the community discovered it unexpectedly also works as a general detail enhancer on realistic footage at 1x resolution with no upscaling. **patientx** got good results in just 1-2 steps. Alisson is now training v2 with 500-1000 paired video samples, inverting real-to-anime pairs for better mouth movement/lipsync fidelity.

- [Anime2Half-Real IC-LoRA compilation: 3 side-by-side comparisons](https://cdn.discordapp.com/attachments/1458032975982755861/1491416463095894248/anime2half_compilation_v2.mp4?ex=69d79d51&is=69d64bd1&hm=af2efeab86b88f5acab470810110ebe098220a1c99a404a69e7ec6a24ac04bdf&)

### Realism Enhancement with LoRAs Only -- protector131090
**protector131090** demonstrated a dramatic before/after comparison -- no LoRA vs. a LoRA trained on just 4 Seedance 2.0 videos for only 1000 steps. "Already big difference. I wonder if LTX 2 just lacks proper training on all of the good movies..." (19 reactions).

- [No LoRA](https://cdn.discordapp.com/attachments/1309520535012638740/1481374747886620743/output_00071_.mp4?ex=69b5b83e&is=69b466be&hm=58e595342db91cdf3345f5a73443fa6569beab7a4121fbedbb3032d9d3563ea3&) vs [With LoRA (1000 steps, 4 videos)](https://cdn.discordapp.com/attachments/1309520535012638740/1481374748398063636/output_00077_.mp4?ex=69b5b83e&is=69b466be&hm=ac75eb474a0a4edcf79b2c6b3d1676bf79be8791565ba69615f030f8ea7cb56d&) (19 reactions)

### Fantasy Puppet -- VRGameDevGirl84
**VRGameDevGirl84** trained and released a fantasy puppet style LoRA (5 reactions), with **lapaing2186** testing it to great results (3 reactions).

- [Fantasy Puppet LoRA sample](https://cdn.discordapp.com/attachments/1457981700817817620/1491882310214225950/video_00002-audio.mp4?ex=69d94f2c&is=69d7fdac&hm=6e8b55e233ffed4d9a7a6b027499d78ec47dc572bd360ca366969a33129567bb&) (5 reactions)
- Model: [civitai.com/models/2530764/ltx-23-fantasy-puppet](https://civitai.com/models/2530764/ltx-23-fantasy-puppet)

### Realism Enhancer LoRAs -- VRGameDevGirl84
**VRGameDevGirl84** trained separate enhancer LoRAs for close-up faces and upper body shots -- no trigger words needed, just apply and quality goes up. "Testing my new enhancer lora finally, even included strange ones. No cherry picking." (6 reactions on the release).

- [Enhancer LoRA samples (no cherry picking)](https://cdn.discordapp.com/attachments/1458032975982755861/1492311350624387283/video_00007-audio.mp4?ex=69dadebf&is=69d98d3f&hm=72f0ca2806093b3537e1a8b38a53ecbd4b9905d0a83eb92beb086b87c71a9102&) (4 reactions)
- [Enhancer sample 2](https://cdn.discordapp.com/attachments/1458032975982755861/1492311351379365919/video_00001-audio.mp4?ex=69dadebf&is=69d98d3f&hm=73889a893bf5485eda65d09e083d7b1adb749360e3edbc767501cb42ac405ecc&)
- [Enhancer sample 3](https://cdn.discordapp.com/attachments/1458032975982755861/1492311352302375003/video_00002-audio.mp4?ex=69dadec0&is=69d98d40&hm=fd8b4ab3809fa4d4e073c1cdee83032c0d6e3b6c9047dd4080b84a3fd91863d3&)
- Model: [civitai.com/models/2535622](https://civitai.com/models/2535622?modelVersionId=2849716)

### 2D Cel Animation -- The Shadow (NYC)
**The Shadow (NYC)** developed a custom 2D LoRA for traditional western cel animation style, and also shared powerful prompt engineering for achieving 2D looks without a LoRA: "cellinework, traditional western 2d cel animation, hand-inked outlines, painted cel fills, medium line weight, subtle contour wobble..."

- [The Shadow's 2D cel animation test](https://cdn.discordapp.com/attachments/1309520535012638740/1488927193491964067/2d_test_00008-audio.mp4?ex=69ce8f01&is=69cd3d81&hm=cc6a69388de8c8a284cab39564b439c0f1a4ede35acc4e94b292a262c1c6c9e5&)

### Color Grading Removal -- oumoumad
**oumoumad** trained an IC-LoRA specifically to *remove* color grading from footage. Compared LTX 2.0 (looked good at ~1500 steps) vs LTX 2.3 (better convergence at higher steps with Prodigy optimizer). "My latest test worked well, performs even better than LTX2.0 at times -- look at the Blade Runner, Batman, The Grand Budapest elevator scenes."

- [Color grading removal: all comparisons reel](https://cdn.discordapp.com/attachments/1457981700817817620/1491122577903456407/all_comparisons.mp4?ex=69d68b9e&is=69d53a1e&hm=e16b0d299e661bc3d10f7b618ad60eab6cc19a356032c29ea4ec8fb298e7387f&) (2 reactions)

### Paper Cut-Out -- VRGameDevGirl84
**VRGameDevGirl84** completed a paper cut-out style LoRA at 9000 steps.

- [Paper cutout LoRA sample 1](https://cdn.discordapp.com/attachments/1458032975982755861/1491443041083326575/papercutout_00005-audio.mp4?ex=69d7b612&is=69d66492&hm=e875b1a318634a43f0e6c0089314440268b4b3d4ac2e8c0d2bcf8f1ed2b05025&)
- [Paper cutout sample 2](https://cdn.discordapp.com/attachments/1458032975982755861/1491443042119323739/papercutout_00004-audio.mp4?ex=69d7b612&is=69d66492&hm=a0238b6d2158e08ae8d4d85fed2b0d525ec1733201d7a7b44f82b4829e8aa5cf&)

### Dark Fantasy -- VRGameDevGirl84
**VRGameDevGirl84** trained a dark fantasy painterly LoRA.

- [Dark fantasy LoRA generation](https://cdn.discordapp.com/attachments/1458032975982755861/1490744431282880782/AnimateDiff_00283-audio.mp4?ex=69d52b71&is=69d3d9f1&hm=b4daccc3adb8f3e7e7a2650b2917ec0b5845b2280deeb0ddd83fea71d92bf590&)

### Golden Age Comics: No Lora vs With Lora -- VRGameDevGirl84
Before/after of the Golden Age Comic LoRA -- "Just testing my new Golden Age Comic LoRa i trained overnight" (5 reactions on the first batch of 10 samples).

- [No LoRA baseline](https://cdn.discordapp.com/attachments/1457981700817817620/1492134499205648575/NoLora.mp4?ex=69da3a0b&is=69d8e88b&hm=00a6a53b490ec824a663e024c05734edc8d906bef1ce1a95e4438a82aa441e95&) vs [Golden Age Comic LoRA applied](https://cdn.discordapp.com/attachments/1457981700817817620/1492134500778508482/video_00001-audio.mp4?ex=69da3a0b&is=69d8e88b&hm=1ed8705f21d1d8a0c5fd485408b28a3f15fca5b57bfd61d3dfb70d845e8f6a35&)
- [Golden Age test batch: 10 samples, no cherry picking](https://cdn.discordapp.com/attachments/1458032975982755861/1492147280436789268/video_00001-audio.mp4?ex=69da45f2&is=69d8f472&hm=19bb1db4d9cb87b6fa7c2a8824820e6ec5ce9c3017c2ebe953ca2a4bba5f7ffc&) (5 reactions)

### Other Notable Style LoRAs
- **VRGameDevGirl84** -- also trained felt cutout animation and marionette style LoRAs
- **David Snow** -- quality improvement LoRAs trained on high-quality datasets

Key insight from **oumoumad**: he trains at **rank 128** even for normal LoRAs because lower ranks showed a big gap in concept capture.

---

## 3. Audio-Visual Generation: Pioneering a New Mode of Control

LTX 2.3's native audio generation sets it apart from every other open-source video model. The community has pushed this in remarkable directions.

### Native Audio-to-Video with Lipsync -- fredbliss
**fredbliss** described the magic: "just tell ltx2 'both people sing' and pass the audio/video latents together and it figures the fuckin thing out. wild." After his first week with LTX 2.3 he called it "what a wild time to be alive" and described music videos "that just can't be done with anything else" running on his 4090.

- [fredbliss music video made entirely with LTX 2.3 audio-visual pipeline](https://cdn.discordapp.com/attachments/1458032975982755861/1490729933817909430/dirkvid.mp4?ex=69d51df0&is=69d3cc70&hm=ec3a7e54af975cef69aedc57df56521f2f2e6e76cecfcced5fc602fb1ef4ecf5&)

### Precision Lipsync via Inpainting -- Nekodificador
**Nekodificador** developed a workflow for high-quality lipsyncing using DaVinci Resolve for precise mouth and chin masking, combined with native LTX inpainting. The crop-and-stitch approach processes the mouth region at 1024 resolution then pastes back onto the stabilized original.

The critical discovery: **the exponential scheduler significantly outperforms all others for lipsync**. Nekodificador explained that LTX averages motion at high sigmas, especially in the last steps -- "schedulers with high sigmas along the last steps stabilize the motion so much that [they] erase the lipsync movement." Using high sigmas only at generation start preserves mouth motion without creating detail blobs.

- [Nekodificador LTX generation: no LoRA vs reasoning LoRA comparison](https://cdn.discordapp.com/attachments/1309520535012638740/1492901054604513390/LTX2_NKD__00185-audio.mp4?ex=69dd03f4&is=69dbb274&hm=f8dc3cad6a0236495fed9c21b466bfe797a7e85d99f038f2e2a6be8ea944e29e&) vs [with reasoning LoRA](https://cdn.discordapp.com/attachments/1309520535012638740/1492901056470974514/LTX2_NKD__00304-audio.mp4?ex=69dd03f4&is=69dbb274&hm=a45aaed754847a8a736866567767e1b18f627dd656df4beb966d9b77f6ef5c8e&)

### Audio Looping and Tooling -- ckinpdx & fredbliss
**ckinpdx** adapted Kijai's latent flow looping workflows to handle audio, building custom LTX utility nodes for trimming audio latents (noting "I couldn't find another node that trims LTX audio latents"). **fredbliss** released ComfyUI-AudioLoopHelper for automated timing calculations in audio/video looping.

- [ckinpdx FADE Director: agentic music video pipeline demo](https://cdn.discordapp.com/attachments/1309520535012638740/1483208992162451688/FADE_Director_example.mp4?ex=69b9c184&is=69b87004&hm=6bc509164bd22c48ae560f2f9a9dd370227172ceb247378fa70d31081c198d10&) (3 reactions)

### Cross-Model Lipsync Preservation -- ckinpdx
**ckinpdx** demonstrated that Wan 2.2 can refine LTX outputs while maintaining lipsync: "you can refine with wan 2.2 and not lose lipsync." This opens up multi-model pipelines where LTX handles audio generation and other models enhance visuals.

- [LTX generation refined with Wan 2.2, lipsync preserved](https://cdn.discordapp.com/attachments/1458032975982755861/1489019319126855710/AnimateDiff_00014-audio.mp4?ex=69cee4ce&is=69cd934e&hm=06e2be92ff72bd44a06c9cebea579c582ade2c0aa51aa6a0af7b3f35a7682960&) (2 reactions)

---

## 4. Long-Form Video: Breaking the 15-Second Barrier

LTX natively generates ~5-15 second clips, but the community has built sophisticated systems for much longer content.

### Unlimited-Length Automation -- VRGameDevGirl84
**VRGameDevGirl84** created the NanoBanana workflow suite that chains LTX generations for arbitrary-length videos with her custom nodes: "I have custom nodes that chain vids together. Unlimited length." **A_Poet** used it for a full music video and reported: "It seems NanoBanana and LTX collaborate well."

**WackyWindsurfer** used it to create a full-length music video -- "No remakes, no edits. Straight from WF" (6 reactions):

- [Full music video, straight from workflow](https://cdn.discordapp.com/attachments/1458032975982755861/1484631443920654509/Bananas_and_APIs.mp4?ex=69beee47&is=69bd9cc7&hm=040baf02d852104a7cdaffcb9c3e7a96458833b75251179d03cfc1579185bd80&) (6 reactions)

### Latent-Space Looping -- Kijai, Jonathan & ckinpdx
**Kijai** and others developed looping samplers that keep generations in latent space end-to-end, avoiding decode/re-encode quality loss. **Jonathan** found the fix for loop artifacts: adding LTXVCropGuides before the LTXVConcatAVLatent node eliminates the "jump to first frame" issue at loop boundaries. **ckinpdx** discovered IC-LoRAs work through the looping sampler, making "the length limit the cap of the load video node" (4 reactions).

- [IC-LoRA applied through looping sampler for extended video](https://cdn.discordapp.com/attachments/1309520535012638740/1491888808201945288/ComfyUI_00048_.mp4?ex=69d95539&is=69d803b9&hm=89c22e162c8e0ebb2501c61f1424784dd42be9f0706ffad7bfd65053b1f97656&) (4 reactions)

### Context Windows -- Ablejones
**Ablejones** built native context window support with controllable sizing and overlap, demonstrating T2V with canny control across 4-5 windows showing "better consistency than most other video models."

- [Ablejones context window demo: T2V with canny across multiple windows](https://cdn.discordapp.com/attachments/1309520535012638740/1491196112357298197/LTX2_Stage2_00021-audio.mp4?ex=69d6d01a&is=69d57e9a&hm=fc9aee5e295b7e345d49c66ac3d2089de51013dd1f77abc46fc2c4c74454187e&)

### Multi-Minute Chaining
Community members reported achieving ~2 minutes by chaining first-frame/last-frame continuations. **Duckers McQuack** was "flabbergasted" by the technique: "extending videos practically to unlimited lengths, where it uses x amount of existing frames as the buffer to resume for further extending."

---

## 5. Music Video Production: Pushing the Boundaries of What's Possible Locally

LTX's combination of speed, audio awareness, and LoRA flexibility has made it a music video powerhouse.

### Music Videos on a 4090 -- fredbliss
**fredbliss** used a modified version of ckinpdx's looping workflow to create a music video from a "failed" 3D print that became a character in a Silence of the Lambs pastiche. Music made from a 15s Sora clip turned into a full song with Lyria 3. "love the music videos you can create with local that just can't be done with anything else."

- [Music video: Big Head Man in Silence of the Lambs](https://cdn.discordapp.com/attachments/1458032975982755861/1490729933817909430/dirkvid.mp4?ex=69d51df0&is=69d3cc70&hm=ec3a7e54af975cef69aedc57df56521f2f2e6e76cecfcced5fc602fb1ef4ecf5&)

### Agentic Music Video Director -- ckinpdx
**ckinpdx** built an agentic app (FADE Director) for automated music video creation. Takes a song, lyrics, and reference image as input -- an LLM directs image and prompt generation. Full pipeline running on his 5090 + 96GB setup, with HuMo integration for motion (9 reactions on his workflow suite).

- [FADE Director agentic pipeline demo](https://cdn.discordapp.com/attachments/1309520535012638740/1483208992162451688/FADE_Director_example.mp4?ex=69b9c184&is=69b87004&hm=6bc509164bd22c48ae560f2f9a9dd370227172ceb247378fa70d31081c198d10&) (3 reactions)

### VRGameDevGirl84's Music Video Production Pipeline
**VRGameDevGirl84** is the architect behind much of the community's music video tooling. She maintains a ComfyUI workflow that automates music video generation end-to-end, using LLM API nodes to interpret prompts and produce structured outputs controlling downstream nodes. She applied for a microgrant to replace the API models with a locally fine-tuned open-source model.

Her NanoBanana/Zimage-LTX workflow suite chains generations for unlimited-length videos. All her workflows are publicly available at [github.com/vrgamegirl19/comfyui-vrgamedevgirl](https://github.com/vrgamegirl19/comfyui-vrgamedevgirl/tree/main/Workflows). She also trained a LoRA on herself (43 photos, overnight, 7500 steps) to test likeness preservation -- **Quality_Control** said he was "shocked how fast it learns an input likeness."

**WackyWindsurfer** used her workflows to create a full-length music video -- "No remakes, no edits. Straight from WF" (6 reactions):

- [Full music video, straight from workflow](https://cdn.discordapp.com/attachments/1458032975982755861/1484631443920654509/Bananas_and_APIs.mp4?ex=69beee47&is=69bd9cc7&hm=040baf02d852104a7cdaffcb9c3e7a96458833b75251179d03cfc1579185bd80&) (6 reactions)

Pom highlighted another "single-shot music video gen using LTX + NBP" as "extremely impressive" (21 reactions):

- [Single-shot LTX music video](https://cdn.discordapp.com/attachments/1458032975982755861/1472357874058530866/finalvideo.mp4?ex=699e2520&is=699cd3a0&hm=40f7e0803db38fe26db8f2d03aefb294e09fe576f902363b7a51005e3673dcc4&)

---

## 6. Training Innovation: Making LoRA Training Accessible to Everyone

The community hasn't just *used* LTX -- they've pushed the boundaries of how to train it.

### In-ComfyUI LoRA Training -- VRGameDevGirl84
**VRGameDevGirl84** built a workflow that trains LoRAs step-by-step right inside ComfyUI: auto-resumes from latest saved state, creates preview videos at each save point, and builds a final labeled XYZ comparison video when training completes. Dataset prep, cache reuse, config generation, training, and LoRA loading all in one graph. 41 reactions -- one of the most-reacted technical posts ever.

- [In-ComfyUI training workflow demo](https://cdn.discordapp.com/attachments/1483092931651965071/1483092944775807036/Recording_2026-03-16_081450.mp4?ex=69b95570&is=69b803f0&hm=5d656280da67290365bbf73ba5ccad654b940511ad8b619e47451d82a2ca63e6&)
- [XYZ comparison output at step 4000](https://cdn.discordapp.com/attachments/1483092931651965071/1483092960990990538/Elven_XYZ_COMPARE_step4000_v2.mp4?ex=69b95574&is=69b803f4&hm=9bdf68a4882320f0cab9c4b5589374d54b16b6854986b7ae67061d38e1a2831e&) (41 reactions combined)

### LLM-Driven Dataset Generation -- VRGameDevGirl84
**VRGameDevGirl84** created a workflow using LLM nodes and custom instructions to generate consistent-style datasets without reference images. "I use this workflow I created. It uses an LLM node and custom LLM instructions to create the same style. You would just give chat gpt the current LLM instructions and tell it to swap out the art style with one you want."

### IC-LoRA Training Pipeline -- Community Effort
The community collectively worked out the full IC-LoRA training pipeline. Key contributors: **oumoumad** (training methodology and reverse-pair insight), **Alisson Pereira** (shared example configs, explained lower LoRA ranks prevent learning unwanted patterns), **JonkoXL** (testing and integration).

Key parameters: `control_directory` in dataset.toml, `training_strategy: name: "video_to_video"` in config, `--lora_target_preset v2v` flag.

### Consumer Hardware Training -- SmaX & Community
**SmaX** documented efficient training setups: 1-2 iterations/second on RTX Pro 6000 (96GB) at $1.89/hour for rank 64 LoRAs at 768x512 @ 97 frames. **siraxe** identified 97-frame clips at 512/768 as the sweet spot for video-only training on 5090s. The community mapped VRAM tiers from 3060 to 5090, making training accessible at every level. **SmaX** recommended disabling sampling during training to save time, instead using loss graphs and manual testing.

### Audio Training Discoveries -- Community
Critical findings about audio-video LoRA training:
- Audio training slows iteration speed from 11s to 30s per step
- Separate audio learning rates at 1/5 to 1/2 of video rates prevent audio overtraining
- Image-only training on audio-video checkpoints causes **complete loss of audio knowledge**
- **JonkoXL** discovered image-only training kills motion, but pruning the overtrained LoRA with uniform 0.5 successfully restores video generation

---

## 7. Speed and Efficiency: The Fastest Open-Source Video Model, Pushed Further

LTX's speed advantage is structural, not marginal, and the community has exploited it aggressively.

### 64 FPS Native -- protector131090
**protector131090** pushed LTX2 to 64fps generation. Pom featured it in his weekly update (32 reactions). Higher fps was found to actually reduce motion artifacts and smudge -- a 50fps native render showed reduced smudge compared to lower framerates.

- [protector131090's 64fps LTX2 generation](https://cdn.discordapp.com/attachments/1309520535012638740/1473614204467679282/LTX-2_00364_.mp4?ex=69997cac&is=69982b2c&hm=df07bb6a5992d5a50fb4acc0cdffce253fc7151450a800c03aa1cb05c6854484&)
- [protector131090 lion generation, #Waiting4LTX3](https://cdn.discordapp.com/attachments/1458032975982755861/1486004354166161521/24FPSLIONLOWQ.mp4?ex=69c3ece6&is=69c29b66&hm=1143cafab9c28e86d9f1ac497b8715a03a3f6463977fa9c9f3dfe0bcb3646e30&) (21 reactions)

### Kijai's 262-Second Generation
**Kijai** shared a generation that ran in 262.62 seconds -- and the result spoke for itself (19 reactions, 15 unique):

- [Kijai LTX2 generation](https://cdn.discordapp.com/attachments/1309520535012638740/1479852179916329063/LTX-2_00010-audio1.mp4?ex=69b61cfe&is=69b4cb7e&hm=da8f8f1f642a3f5a0425326f7efab0845e994333d09c787bbe5d229d5b23eeb5&) (19 reactions)

### Three-Pass Optimization -- garbus & mdkb
**garbus** and **mdkb** refined three-pass LTX 2.3 workflows achieving 25-minute end-to-end renders on a **3060 GPU** for 241 frames at 24fps. Starting at 0.25 resolution and adding upscale passes gave ~3x speed improvements and faster detection of bad generations, with some noting improved motion fluidity as a bonus.

### 8-Step Generation -- hicho
**hicho** demonstrated using LTX with a simple single-KSampler workflow at just 8 steps, getting usable output:

- [8-step LTX generation](https://cdn.discordapp.com/attachments/1458032975982755861/1492584653016666172/ComfyUI_00081_.mp4?ex=69dbdd48&is=69da8bc8&hm=6a8ac1d67ce5a794596cfef4f2fe775b63114eda406fd0d724464cbd4f2d00be&)

### AnimateDiff-Style V2V at Speed -- hicho
**hicho** used LTX 2.3's v2v latent pipeline to produce "modern AnimateDiff-like video" -- achieving the aesthetic people loved from AnimateDiff but with LTX's speed and quality:

- [AnimateDiff-style v2v with LTX 2.3](https://cdn.discordapp.com/attachments/1142935168584265779/1492229370520010873/AnimateDiff_00064-audio.mp4?ex=69da9266&is=69d940e6&hm=4a50259bba93bb63087f506f7b86155fd3403c62ace682acc2742380de029da6&)

---

## 8. Cross-Model Pipelines: LTX as the Backbone of Multi-Model Workflows

LTX has become a key piece in multi-model workflows, and the community's pipeline innovation is arguably as impressive as any single generation.

### Multi-Pass Quality Stacking -- ckinpdx
**ckinpdx** described a sophisticated pipeline for production-quality output: "upscale to 4k in precise 2.5, do a 1080p upscale in one of the faster starlight models that scrubs all the texture details, then do a blend between the base gen, the scrubbed textureless upscale at a lower percent and the precise 2.5 at ~60% ish, shot depending, basically a mixed blend of 3 versions." Sometimes a 4th layer using edge detection as a mask to overlay only the sharp edges from the high-quality upscale.

### LTX + Seedance 2 Outpainting -- Purz
**Purz** demonstrated using LTX 2.3 to outpaint Seedance 2 outputs: "using ltx2.3 to outpaint seedance 2 is funny" -- leveraging LTX's canvas extension IC-LoRA on another model's output.

### BIM-VFI for Cross-Model FPS Matching -- Ethanfel & Gleb Tretyak
**Ethanfel** shipped ComfyUI-Tween with BIM-VFI interpolation. **Gleb Tretyak** used it specifically to convert Wan output to LTX's 25fps: "yooo yep it worked quite well! I needed to convert wan output to ltx. worked." **patientx** reported speed improvements from 380s to 320s with torch.compile.

### Custom ComfyUI Nodes for LTX Workflows -- Jonathan
**Jonathan** built a custom node pack (WhatDreamsCost Nodes) including a Multi Image Loader with gallery, auto-resize, and LTXVPreprocess integration -- specifically designed to streamline LTX multi-image workflows (19 reactions):

- [WhatDreamsCost Nodes trailer](https://cdn.discordapp.com/attachments/1484614256602386663/1484614259307581450/WhatDreamsCost_Nodes_Trailer.mp4?ex=69bede46&is=69bd8cc6&hm=3f8907d7bad7bc49f95fbd6d9f9afdbb8b42b7209f55809ad6493671b4ffe2b3&) (19 reactions)

---

## 9. The Arca Gidan Prize: Art Competition at the Frontier of AI Video

The Arca Gidan Prize (Edition II) showcased LTX and other tools pushed to their artistic limits -- 95 entries, 7,285 votes, on the theme of "Time."

### Everyone All at Once -- visualfrisson
**visualfrisson** used technical tricks to address the theme of time powerfully, showing the "endless, timeless quality to hip-hop lyrics that we might not always appreciate." Pom called it "typically incredibly impressive - not just technically, but also how it uses the technical trick to address the theme really powerfully" (39 reactions).

- Entry viewable on [arcagidan.com](https://arcagidan.com)

### Archive 113391 -- yuvraj108c
**yuvraj108c** used audio reactivity to tell a second story layered on top of the main one -- "The video kind of dances between them in time with the music and it's excellently done" (25 reactions).

- [Archive 113391](https://cdn.discordapp.com/attachments/1316024582041243668/1490075690157211758/yuvraj108c.mp4?ex=69d2bca0&is=69d16b20&hm=7b8fb568473ef808a23b642db5fc89ccbf8314527746aa)

### Cherrybomb (Original Music) -- sagansagansagans
**sagansagansagans** combined traditional narrative structure with an abstract, audio-driven transformation that "kind of extracts you out of the main story into the emotional depth it's trying to communicate" (23 reactions).

- Entry viewable on [arcagidan.com](https://arcagidan.com) (23 reactions)

### Synth Study #1 -- gorkulus
**gorkulus** gave us "Windows Media Player 2035" -- a completely different aesthetic lane (21 reactions).

- [Synth Study #1](https://cdn.discordapp.com/attachments/1316024582041243668/1490076584764506413/gorkulus.mp4?ex=69d2bd76&is=69d16bf6&hm=d948a9ccf099cfe4be6d77fe5670393811efafc3c19e524e135b2622b50b214d&)

### It can't be that bad -- Victoria
**Victoria** captured "an experience many men hear about but are fortunate enough to never experience -- and captures it in such a visceral and relatable way" (25 reactions).

- [It can't be that bad](https://cdn.discordapp.com/attachments/1316024582041243668/1489726205723676782/viktoria_vw0.mp4) (25 reactions)

### Bosch-esque Tapestry -- tom_scaria
Not a single generation but dozens of runs through various tools combined into a looping, detail-rich tapestry. Pom: "takes all kinds of unusual twists and turns along the way to end up something completely new, original and engaging" (20 reactions).

- [Bosch-esque Tapestry](https://cdn.discordapp.com/attachments/1316024582041243668/1489726690300268594/tom_scaria0.mp4?ex=69d17798&is=69d02618&hm=38bd95b4801dbe4f4950f6f8)

---

## 10. Fun & Viral: The Videos That Made Everyone Lose It

Not everything has to be a technical breakthrough. Some of the most-reacted content in the community has been just... fun.

### "THE DANGER" feat. Inflated LoRAs -- ingi // SYSTMS
**ingi // SYSTMS** created "THE DANGER" using inflated LoRAs and it became one of the most iconic community videos. Pom featured it in an Art Appreciation Tuesday to 78 reactions.

- [THE DANGER HD](https://cdn.discordapp.com/attachments/1344057524935983125/1460356536181198888/THE_DANGER_HD.mp4) (78 reactions)

### Inflating Bad -- ingi // SYSTMS
Walter White, but inflated. 64 reactions. No further explanation needed.

- [INFLATING BAD](https://cdn.discordapp.com/attachments/1344057524935983125/1461050645539717161/INFLATING_BAD_01.mp4) (64 reactions)

### Old Scatman -- community
The scatman video that somehow got 48 reactions:

- [Old Scatman](https://cdn.discordapp.com/attachments/1309520535012638740/1461492613206245406/oldscatman.mp4) (48 reactions)

### thankskijai -- Dj47 & Godhand
**Dj47** created "thankskijai" -- a tribute to Kijai's contributions to the community that became a viral hit at 81 reactions. **Godhand** later reposted it asking for workflow tips and got another 12 reactions.

- [thankskijai](https://cdn.discordapp.com/attachments/1309520535012638740/1461772498902319167/thankskijai01.mp4) (81 reactions)

### NebSH's Lipsync Demo
**NebSH** shared an early lipsync generation that made people's jaws drop -- 28 reactions:

- [NebSH lipsync demo](https://cdn.discordapp.com/attachments/1309520535012638740/1460288910297923767/Lipsync2Sampler_00016-audio.mp4?ex=697e1a87&is=697cc907&hm=3231f6cb5fc8a2b5506ff3e1b8adbd394aa1b5ea9c96e5bbb7ec2862c4e0c391&) (28 reactions)

---

## The Community Members Who Push Things Forward

All of this is happening on consumer hardware. People are training LoRAs on 3060s and 5090s, generating music videos on 4090s, and building feature-film production pipelines in ComfyUI. LTX's position as the fastest open-source video model, combined with its native audio support and IC-LoRA extensibility, has created an ecosystem where individual creators are pushing capabilities that didn't exist months ago.

As **oumoumad** put it: "LTX is just so good at context, I've been constantly impressed with things I've seen, like respecting depth of field, FOV etc... And I'm genuinely impressed some people got to do anime with it, another thing I didn't have in my dataset lol."

Or as **fredbliss** said after his first week with LTX 2.3: "what a wild time to be alive."

### Key People

- [![oumoumad](https://cdn.discordapp.com/avatars/257217392298426380/9aac8eacc689882e3627234cb232053b.png?size=32)](https://cdn.discordapp.com/avatars/257217392298426380/9aac8eacc689882e3627234cb232053b.png?size=128) **oumoumad** -- Outpaint IC-LoRA, depth IC-LoRA, ReFocus IC-LoRA, color grading removal, style transfer methodology, IC-LoRA training insights
- [![Alisson Pereira](https://cdn.discordapp.com/avatars/211685818622803970/6afbbf76e08e3844329d5804bf87ee72.png?size=32)](https://cdn.discordapp.com/avatars/211685818622803970/6afbbf76e08e3844329d5804bf87ee72.png?size=128) **Alisson Pereira** -- BFS face/head swap, anime2half-realism, MR2V masked inpainting, IC-LoRA training configs
- [![VRGameDevGirl84](https://cdn.discordapp.com/avatars/330829305477333022/30fa6781db882a5380bdda729421d6a8.png?size=32)](https://cdn.discordapp.com/avatars/330829305477333022/30fa6781db882a5380bdda729421d6a8.png?size=128) **VRGameDevGirl84** -- Pixar/Golden Age/puppet/felt/paper style LoRAs, in-ComfyUI training workflow, NanoBanana unlimited-length system, LLM dataset generation, music video pipeline
- [![Kijai](https://cdn.discordapp.com/avatars/228118453062467585/2ac19c5c44d8f8d8a6de84fad038b930.png?size=32)](https://cdn.discordapp.com/avatars/228118453062467585/2ac19c5c44d8f8d8a6de84fad038b930.png?size=128) **Kijai** -- Looping sampler, SAM3 integration, ComfyUI core infrastructure, RIFE/FILM optimization
- [![ckinpdx](https://cdn.discordapp.com/avatars/1166951985573003363/1aceb1f919a915174d34ebe10f3d2ee4.png?size=32)](https://cdn.discordapp.com/avatars/1166951985573003363/1aceb1f919a915174d34ebe10f3d2ee4.png?size=128) **ckinpdx** -- Audio looping workflows, music video pipelines, HuMo integration, multi-pass quality stacking
- [![Ablejones](https://cdn.discordapp.com/avatars/256636116763934731/762f9b4513bf17beee4069a0f8c0ae10.png?size=32)](https://cdn.discordapp.com/avatars/256636116763934731/762f9b4513bf17beee4069a0f8c0ae10.png?size=128) **Ablejones** -- Context window management, detailer IC-LoRA techniques, native context windows
- [![Cseti](https://cdn.discordapp.com/avatars/1074404980737450065/3503243a4cf6ee9cda41262ef47be10f.png?size=32)](https://cdn.discordapp.com/avatars/1074404980737450065/3503243a4cf6ee9cda41262ef47be10f.png?size=128) **Cseti** -- Camera motion transfer IC-LoRA
- [![siraxe](https://cdn.discordapp.com/avatars/265203531969986560/bb52cd9669352e38d6e7b43625553ab1.png?size=32)](https://cdn.discordapp.com/avatars/265203531969986560/bb52cd9669352e38d6e7b43625553ab1.png?size=128) **siraxe** -- MergeGreen IC-LoRA, TTM IC-LoRA
- [![Nekodificador](https://cdn.discordapp.com/avatars/391020191338987522/2e40c243a0ff35f557eef85c02a4b507.png?size=32)](https://cdn.discordapp.com/avatars/391020191338987522/2e40c243a0ff35f557eef85c02a4b507.png?size=128) **Nekodificador** -- Precision lipsync workflow, exponential scheduler discovery
- [![protector131090](https://cdn.discordapp.com/avatars/781117147421474837/11ffb3dcc89eb1116ccd62894e85a369.png?size=32)](https://cdn.discordapp.com/avatars/781117147421474837/11ffb3dcc89eb1116ccd62894e85a369.png?size=128) **protector131090** -- 64fps generation, pushing LTX visual limits
- [![crinklypaper](https://cdn.discordapp.com/avatars/138234075931475968/c57eadbf685d3058ba57443da3ee72e7.png?size=32)](https://cdn.discordapp.com/avatars/138234075931475968/c57eadbf685d3058ba57443da3ee72e7.png?size=128) **crinklypaper** -- Extended anime LoRA training (34k+ steps)
- [![fredbliss](https://cdn.discordapp.com/avatars/606012921935429632/0b768c1e00e2f061ab07c205df9fe54d.png?size=32)](https://cdn.discordapp.com/avatars/606012921935429632/0b768c1e00e2f061ab07c205df9fe54d.png?size=128) **fredbliss** -- Music video production, AudioLoopHelper
- [![Jonathan](https://cdn.discordapp.com/avatars/1198356571906904255/cb000b444b8a914a83f9b160b7c220f4.png?size=32)](https://cdn.discordapp.com/avatars/1198356571906904255/cb000b444b8a914a83f9b160b7c220f4.png?size=128) **Jonathan** -- WhatDreamsCost custom nodes, loop fix with CropGuides
- [![hicho](https://cdn.discordapp.com/avatars/1279467736187146242/7e25bd6fe015f7dac8dbb138713c0c63.png?size=32)](https://cdn.discordapp.com/avatars/1279467736187146242/7e25bd6fe015f7dac8dbb138713c0c63.png?size=128) **hicho** -- 8-step generation, AnimateDiff-style v2v
- [![SmaX](https://cdn.discordapp.com/avatars/179640884311097344/e715e382e462de820d9cf9a83f9c3c88.png?size=32)](https://cdn.discordapp.com/avatars/179640884311097344/e715e382e462de820d9cf9a83f9c3c88.png?size=128) **SmaX** -- Training efficiency documentation across GPU tiers
- [![N0NSens](https://cdn.discordapp.com/avatars/867727155499499520/7206e3347f00016ae9bc414f8193f241.png?size=32)](https://cdn.discordapp.com/avatars/867727155499499520/7206e3347f00016ae9bc414f8193f241.png?size=128) **N0NSens** -- Cinema4D depth map integration
- [![Ethanfel](https://cdn.discordapp.com/avatars/155631980749389824/b3de2e467fa8324802be5df4483d0807.png?size=32)](https://cdn.discordapp.com/avatars/155631980749389824/b3de2e467fa8324802be5df4483d0807.png?size=128) **Ethanfel** -- BIM-VFI interpolation, SelVA audio, ComfyUI tooling
