import { describe, expect, it } from "vitest";
import { autoProfilePatch } from "./autoProfilePatch";

describe("automatic profile partial updates", () => {
  it("does not send untouched unsupported fields when changing ASR or dubbing", () => {
    const before = JSON.stringify({formats:{srt:true,ass:true},asrModel:"old",autoDubP2:false,autoPublish:false});
    const after = JSON.stringify({formats:{srt:true,ass:true},asrModel:"new",autoDubP2:true,autoPublish:false});
    expect(autoProfilePatch({formats:["srt","ass"],asr_model:"new",auto_dub_p2:true,auto_publish:false},before,after))
      .toEqual({asr_model:"new",auto_dub_p2:true});
  });
  it("sends both caption mode and compatibility boolean", () => {
    expect(autoProfilePatch({youtube_subtitle_mode:"off",prefer_youtube_subtitles:false},
      '{"youtubeSubtitleMode":"target"}','{"youtubeSubtitleMode":"off"}'))
      .toEqual({youtube_subtitle_mode:"off",prefer_youtube_subtitles:false});
  });
  it("does not silently drop an edited field or an unknown mapping", () => {
    expect(autoProfilePatch({formats:["srt"]},'{"formats":{"srt":true,"ass":true}}','{"formats":{"srt":true,"ass":false}}')).toEqual({formats:["srt"]});
    expect(() => autoProfilePatch({future_field:true},"{}","{}")).toThrow();
  });
  it("sends the model alongside a changed provider", () => {
    expect(autoProfilePatch({translate_model_provider_id:"b",translate_model_id:"same"},
      '{"translateModelProviderId":"a","translateModelId":"same"}','{"translateModelProviderId":"b","translateModelId":"same"}'))
      .toEqual({translate_model_provider_id:"b",translate_model_id:"same"});
  });
});
