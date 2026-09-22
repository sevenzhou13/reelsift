// Reelsift 原生 Finder 服务：以真实素材与 Story Beat 为中心的本地导演台。
import AppKit
import AVKit
import Foundation

let projectDirectory = "/Users/seven/AI-agent/reelsift"
let pythonPath = "\(projectDirectory)/.venv/bin/python"
let organizerPath = "\(projectDirectory)/scripts/reelsift_finder_organize.py"
let bridgePath = "\(projectDirectory)/scripts/reelsift_director_bridge.py"

private let clipPasteboardType = NSPasteboard.PasteboardType("com.reelsift.clip")
private let beatPasteboardType = NSPasteboard.PasteboardType("com.reelsift.story-beat")

/// 设计变量：色彩/间距/圆角/字号统一定义，呼应 portfolio/ 里 Reelwave 作品页的品牌色。
/// 背景/正文继续用系统语义色（跟随深色模式），这里只收敛写死的品牌强调色和几何常量。
private enum Theme {
    static let brandLime = NSColor(calibratedRed: 0.788, green: 0.953, blue: 0.416, alpha: 1)
    static let brandForest = NSColor(calibratedRed: 0.043, green: 0.114, blue: 0.075, alpha: 1)
    static let brandOrange = NSColor(calibratedRed: 0.922, green: 0.494, blue: 0.263, alpha: 1)
    static let brandMoss = NSColor(calibratedRed: 0.286, green: 0.396, blue: 0.306, alpha: 1)

    static let space1: CGFloat = 4
    static let space2: CGFloat = 8
    static let space3: CGFloat = 12
    static let space4: CGFloat = 16
    static let space5: CGFloat = 24

    static let radiusChip: CGFloat = 6
    static let radiusCard: CGFloat = 10
    static let radiusPanel: CGFloat = 14

    static let borderAlpha: CGFloat = 0.75

    static func caption() -> NSFont { .systemFont(ofSize: 10, weight: .medium) }
    static func small() -> NSFont { .systemFont(ofSize: 11) }
    static func body() -> NSFont { .systemFont(ofSize: 12.5) }
    static func subhead() -> NSFont { .systemFont(ofSize: 14, weight: .semibold) }
    static func title() -> NSFont { .systemFont(ofSize: 18, weight: .bold) }
    static func largeTitle() -> NSFont { .systemFont(ofSize: 24, weight: .bold) }

    /// 三个方向/编号轮换色，用于方向卡片、故事线编号徽标等需要区分序号的地方。
    static func accent(_ index: Int) -> NSColor {
        [brandLime, brandOrange, brandMoss][index % 3]
    }

    static func applyCardStyle(_ view: NSView, cornerRadius: CGFloat = radiusCard, background: NSColor = .textBackgroundColor) {
        view.wantsLayer = true
        view.layer?.backgroundColor = background.cgColor
        view.layer?.cornerRadius = cornerRadius
        view.layer?.borderWidth = 0.5
        view.layer?.borderColor = NSColor.separatorColor.withAlphaComponent(borderAlpha).cgColor
    }

    static func applyPrimaryButton(_ button: NSButton) {
        button.bezelStyle = .rounded
        button.bezelColor = brandLime
        button.contentTintColor = brandForest
    }
}

final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

// NSTextView 没有原生 placeholder；随手记为空时用克制的提示文本补足输入语义。
final class PlaceholderTextView: NSTextView {
    var placeholderString = "" { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholderString.isEmpty else { return }
        let inset = textContainerInset
        let rect = NSRect(x: inset.width + 2, y: inset.height + 1, width: max(0, bounds.width - inset.width * 2 - 4), height: max(0, bounds.height - inset.height * 2 - 2))
        (placeholderString as NSString).draw(in: rect, withAttributes: [
            .font: font ?? NSFont.systemFont(ofSize: 12),
            .foregroundColor: NSColor.placeholderTextColor,
        ])
    }

    override func didChangeText() {
        super.didChangeText()
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        super.mouseDown(with: event)
    }
}

extension NSView {
    func reelsiftDragImage() -> NSImage {
        guard let bitmap = bitmapImageRepForCachingDisplay(in: bounds) else { return NSImage(size: bounds.size) }
        cacheDisplay(in: bounds, to: bitmap)
        let image = NSImage(size: bounds.size)
        image.addRepresentation(bitmap)
        return image
    }
}

// 桥接升级时缺失字段不应阻断已识别项目打开。
struct DeskClip: Decodable {
    let id: Int; let filename: String; let filepath: String; let previewPath: String; let coverPath: String
    let summary: String; let title: String; let detail: String; let visualTags: [String]; let narrativeTags: [String]
    let userNote: String; let noteStatus: String; let transcript: String
    enum CodingKeys: String, CodingKey { case id, filename, filepath, previewPath = "preview_path", coverPath = "cover_path", summary, title = "rename_title", detail = "detail_summary", visualTags = "visual_tags", narrativeTags = "narrative_tags", userNote = "user_note", noteStatus = "note_status", transcript }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); id = try c.decode(Int.self, forKey: .id); filename = try c.decodeIfPresent(String.self, forKey: .filename) ?? "未命名素材"; filepath = try c.decodeIfPresent(String.self, forKey: .filepath) ?? ""; previewPath = try c.decodeIfPresent(String.self, forKey: .previewPath) ?? ""; coverPath = try c.decodeIfPresent(String.self, forKey: .coverPath) ?? ""; summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? "暂无 AI 回忆"; let rawTitle = try c.decodeIfPresent(String.self, forKey: .title) ?? ""; title = rawTitle.isEmpty ? (try c.decodeIfPresent(String.self, forKey: .filename) ?? "未命名素材") : rawTitle; let rawDetail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""; detail = rawDetail.isEmpty ? summary : rawDetail; visualTags = try c.decodeIfPresent([String].self, forKey: .visualTags) ?? []; narrativeTags = try c.decodeIfPresent([String].self, forKey: .narrativeTags) ?? []; userNote = try c.decodeIfPresent(String.self, forKey: .userNote) ?? ""; noteStatus = try c.decodeIfPresent(String.self, forKey: .noteStatus) ?? "pending"; transcript = try c.decodeIfPresent(String.self, forKey: .transcript) ?? "" }
}

struct StoryClipLink: Decodable { let clipID: Int; let order: Int; let inPoint: Double?; let outPoint: Double?
    enum CodingKeys: String, CodingKey { case id, clipID = "clip_id", order, position, inPoint = "in_point", outPoint = "out_point", inSeconds = "in_seconds", outSeconds = "out_seconds", inMS = "in_ms", outMS = "out_ms" }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); clipID = try c.decodeIfPresent(Int.self, forKey: .clipID) ?? c.decode(Int.self, forKey: .id); order = try c.decodeIfPresent(Int.self, forKey: .order) ?? c.decodeIfPresent(Int.self, forKey: .position) ?? 0; inPoint = try c.decodeIfPresent(Double.self, forKey: .inPoint) ?? c.decodeIfPresent(Double.self, forKey: .inSeconds) ?? c.decodeIfPresent(Double.self, forKey: .inMS).map { $0 / 1000 }; outPoint = try c.decodeIfPresent(Double.self, forKey: .outPoint) ?? c.decodeIfPresent(Double.self, forKey: .outSeconds) ?? c.decodeIfPresent(Double.self, forKey: .outMS).map { $0 / 1000 } }
    init(clipID: Int, order: Int, inPoint: Double?, outPoint: Double?) { self.clipID = clipID; self.order = order; self.inPoint = inPoint; self.outPoint = outPoint }
}

struct StoryBeat: Decodable { let id: Int; var title: String; var intent: String; var script: String; var userNote: String; let order: Int; let locked: Bool; let aiGenerated: Bool; let clips: [StoryClipLink]
    enum CodingKeys: String, CodingKey { case id, title, intent, script, narration, note, userNote = "user_note", order, orderIndex = "order_index", position, locked, mustUse = "must_use", aiGenerated = "ai_generated", userEdited = "user_edited", clips, linkedClips = "linked_clips", clipIDs = "clip_ids" }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); id = try c.decode(Int.self, forKey: .id); title = try c.decodeIfPresent(String.self, forKey: .title) ?? "未命名 Beat"; intent = try c.decodeIfPresent(String.self, forKey: .intent) ?? ""; script = try c.decodeIfPresent(String.self, forKey: .script) ?? c.decodeIfPresent(String.self, forKey: .narration) ?? ""; userNote = try c.decodeIfPresent(String.self, forKey: .userNote) ?? c.decodeIfPresent(String.self, forKey: .note) ?? ""; order = try c.decodeIfPresent(Int.self, forKey: .order) ?? c.decodeIfPresent(Int.self, forKey: .orderIndex) ?? c.decodeIfPresent(Int.self, forKey: .position) ?? 0; locked = try c.decodeIfPresent(Bool.self, forKey: .locked) ?? c.decodeIfPresent(Bool.self, forKey: .mustUse) ?? false; aiGenerated = try c.decodeIfPresent(Bool.self, forKey: .aiGenerated) ?? !(try c.decodeIfPresent(Bool.self, forKey: .userEdited) ?? false); clips = (try? c.decode([StoryClipLink].self, forKey: .clips)) ?? (try? c.decode([StoryClipLink].self, forKey: .linkedClips)) ?? ((try? c.decode([Int].self, forKey: .clipIDs)) ?? []).enumerated().map { StoryClipLink(clipID: $0.element, order: $0.offset, inPoint: nil, outPoint: nil) } }
}

struct StorySuggestion: Decodable { let id: Int; let title: String; let reason: String; let suggestedIntent: String; let suggestedScript: String
    enum CodingKeys: String, CodingKey { case id, title, reason, explanation, message, suggestedIntent = "suggested_intent", suggestedScript = "suggested_script", intent, script }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); id = try c.decode(Int.self, forKey: .id); title = try c.decodeIfPresent(String.self, forKey: .title) ?? "故事结构可能发生变化"; reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? c.decodeIfPresent(String.self, forKey: .explanation) ?? c.decodeIfPresent(String.self, forKey: .message) ?? "这次素材调整可能改变了该 Beat 的叙事含义。"; suggestedIntent = try c.decodeIfPresent(String.self, forKey: .suggestedIntent) ?? c.decodeIfPresent(String.self, forKey: .intent) ?? ""; suggestedScript = try c.decodeIfPresent(String.self, forKey: .suggestedScript) ?? c.decodeIfPresent(String.self, forKey: .script) ?? "" }
}

struct StoryChatAction: Decodable {
    let suggestionID: Int
    let beatTitle: String
    let explanation: String
    let suggestedIntent: String
    let suggestedScript: String
    enum CodingKeys: String, CodingKey { case type, suggestionID = "suggestion_id", beatTitle = "beat_title", explanation, suggestedIntent = "suggested_intent", suggestedScript = "suggested_script" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        suggestionID = try c.decodeIfPresent(Int.self, forKey: .suggestionID) ?? 0
        beatTitle = try c.decodeIfPresent(String.self, forKey: .beatTitle) ?? ""
        explanation = try c.decodeIfPresent(String.self, forKey: .explanation) ?? ""
        suggestedIntent = try c.decodeIfPresent(String.self, forKey: .suggestedIntent) ?? ""
        suggestedScript = try c.decodeIfPresent(String.self, forKey: .suggestedScript) ?? ""
    }
    init(suggestionID: Int, beatTitle: String, explanation: String, suggestedIntent: String, suggestedScript: String) {
        self.suggestionID = suggestionID; self.beatTitle = beatTitle; self.explanation = explanation; self.suggestedIntent = suggestedIntent; self.suggestedScript = suggestedScript
    }
}

struct StoryChatMessage: Decodable {
    let id: Int
    let role: String
    let content: String
    let reasoningText: String
    let action: StoryChatAction?
    enum CodingKeys: String, CodingKey { case id, role, content, reasoningText = "reasoning_text", action }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(Int.self, forKey: .id) ?? 0
        role = try c.decodeIfPresent(String.self, forKey: .role) ?? "assistant"
        content = try c.decodeIfPresent(String.self, forKey: .content) ?? ""
        reasoningText = try c.decodeIfPresent(String.self, forKey: .reasoningText) ?? ""
        action = try? c.decodeIfPresent(StoryChatAction.self, forKey: .action)
    }
    init(id: Int, role: String, content: String, reasoningText: String = "", action: StoryChatAction? = nil) {
        self.id = id; self.role = role; self.content = content; self.reasoningText = reasoningText; self.action = action
    }
}

struct StoryChatMessagesEnvelope: Decodable { let ok: Bool; let messages: [StoryChatMessage]? }
struct AgentChatEnvelope: Decodable {
    let ok: Bool?
    let message: StoryChatMessage?
    let aiError: String?
    enum CodingKeys: String, CodingKey { case ok, message, aiError = "ai_error" }
}

struct StoryInterpretationRequest: Codable {
    let kind: String
    let storyboardID: Int
    let clipID: Int?
    let sourceBeatID: Int?
    let targetBeatID: Int?
    let beforeClipIDs: [Int]?
    let baseClipIDs: [Int]?
    let baseIntent: String?
    let baseScript: String?
    let beforeBeatIDs: [Int]?
    let baseBeatIDs: [Int]?

    enum CodingKeys: String, CodingKey {
        case kind, storyboardID = "storyboard_id", clipID = "clip_id"
        case sourceBeatID = "source_beat_id", targetBeatID = "target_beat_id"
        case beforeClipIDs = "before_clip_ids", baseClipIDs = "base_clip_ids"
        case baseIntent = "base_intent", baseScript = "base_script"
        case beforeBeatIDs = "before_beat_ids", baseBeatIDs = "base_beat_ids"
    }
}

struct StoryMoveEnvelope: Decodable {
    let suggestion: StorySuggestion?
    let interpretationRequest: StoryInterpretationRequest?
    let interpretation: String?
    let stale: Bool?
    let aiError: String?

    enum CodingKeys: String, CodingKey {
        case suggestion, interpretationRequest = "interpretation_request", interpretation, stale
        case aiError = "ai_error"
    }
}

struct Story: Decodable { let id: Int; let title: String; let thesis: String; let status: String; let beats: [StoryBeat]
    enum CodingKeys: String, CodingKey { case id, storyboardID = "storyboard_id", title, thesis, direction, storyPlan = "story_plan", status, beats }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); id = try c.decodeIfPresent(Int.self, forKey: .id) ?? c.decode(Int.self, forKey: .storyboardID); title = try c.decodeIfPresent(String.self, forKey: .title) ?? "未命名故事"; thesis = try c.decodeIfPresent(String.self, forKey: .thesis) ?? c.decodeIfPresent(String.self, forKey: .direction) ?? c.decodeIfPresent(String.self, forKey: .storyPlan) ?? ""; status = try c.decodeIfPresent(String.self, forKey: .status) ?? "ready"; beats = try c.decodeIfPresent([StoryBeat].self, forKey: .beats) ?? [] }
    init(id: Int, title: String, thesis: String, status: String, beats: [StoryBeat]) { self.id = id; self.title = title; self.thesis = thesis; self.status = status; self.beats = beats }
}

struct StoryDirection: Codable {
    let id: String
    let title: String
    let style: String
    let structure: [String]
}

struct StoryDirectionsEnvelope: Decodable {
    let ok: Bool
    let directions: [StoryDirection]?
    let error: String?
}

struct DeskPayload: Decodable { let folder: String; let libraryID: Int; let memoryNote: String; let items: [DeskClip]; let story: Story?; let unusedClipIDs: [Int]; let pendingSuggestion: StorySuggestion?; let restoreWarning: String
    enum CodingKeys: String, CodingKey { case folder, libraryID = "library_id", memoryNote = "memory_note", items, story, latestStory = "latest_story", beats, unusedClipIDs = "unused_clip_ids", unusedClips = "unused_clips", unused, pendingSuggestion = "pending_suggestion", pendingSuggestions = "pending_suggestions", restoreWarning = "restore_warning" }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); folder = try c.decodeIfPresent(String.self, forKey: .folder) ?? ""; libraryID = try c.decodeIfPresent(Int.self, forKey: .libraryID) ?? 0; memoryNote = try c.decodeIfPresent(String.self, forKey: .memoryNote) ?? ""; items = try c.decodeIfPresent([DeskClip].self, forKey: .items) ?? []; let rawStory = (try? c.decode(Story.self, forKey: .story)) ?? (try? c.decode(Story.self, forKey: .latestStory)); let decodedBeats = try c.decodeIfPresent([StoryBeat].self, forKey: .beats) ?? []; story = rawStory.map { Story(id: $0.id, title: $0.title, thesis: $0.thesis, status: $0.status, beats: decodedBeats.isEmpty ? $0.beats : decodedBeats) }; pendingSuggestion = (try? c.decode(StorySuggestion.self, forKey: .pendingSuggestion)) ?? (try? c.decode([StorySuggestion].self, forKey: .pendingSuggestions))?.first; unusedClipIDs = (try? c.decode([Int].self, forKey: .unusedClipIDs)) ?? ((try? c.decode([DeskClip].self, forKey: .unusedClips)) ?? (try? c.decode([DeskClip].self, forKey: .unused)) ?? []).map(\.id); restoreWarning = try c.decodeIfPresent(String.self, forKey: .restoreWarning) ?? "" }
}

final class ClipCardView: NSView, NSDraggingSource {
    private let clipID: Int
    private let selectAction: () -> Void
    private var mouseDownPoint: NSPoint = .zero
    private var dragStarted = false
    init(clip: DeskClip, status: String, hasCreatorNote: Bool, selected: Bool, select: @escaping () -> Void) {
        clipID = clip.id; selectAction = select; super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = selected ? Theme.brandLime.withAlphaComponent(0.16).cgColor : NSColor.clear.cgColor
        layer?.cornerRadius = Theme.radiusChip
        layer?.borderWidth = selected ? 1 : 0
        layer?.borderColor = Theme.brandLime.withAlphaComponent(Theme.borderAlpha).cgColor
        // 统一使用 16:9 画框：横版画面自然铺满，竖版素材完整居中，不拉伸或裁掉主体。
        let image = NSImageView(); image.imageScaling = .scaleProportionallyUpOrDown; image.wantsLayer = true; image.layer?.backgroundColor = NSColor.tertiaryLabelColor.withAlphaComponent(0.10).cgColor; image.layer?.cornerRadius = 5; image.layer?.masksToBounds = true; image.translatesAutoresizingMaskIntoConstraints = false; image.image = clip.coverPath.isEmpty ? nil : NSImage(contentsOfFile: clip.coverPath)
        let text = NSTextField(wrappingLabelWithString: clip.title); text.font = Theme.small(); text.maximumNumberOfLines = 2; text.lineBreakMode = .byTruncatingTail; text.translatesAutoresizingMaskIntoConstraints = false
        let tagsText = (clip.visualTags + clip.narrativeTags).filter { !$0.isEmpty }.prefix(4).joined(separator: " · ")
        let tagsLine = NSTextField(labelWithString: "  " + (tagsText.isEmpty ? "暂无标签" : tagsText) + "  "); tagsLine.font = NSFont.systemFont(ofSize: 9.5, weight: .medium); tagsLine.textColor = tagsText.isEmpty ? .quaternaryLabelColor : .secondaryLabelColor; tagsLine.lineBreakMode = .byTruncatingTail; tagsLine.wantsLayer = true; tagsLine.layer?.backgroundColor = NSColor.secondaryLabelColor.withAlphaComponent(0.10).cgColor; tagsLine.layer?.cornerRadius = Theme.radiusChip; tagsLine.layer?.masksToBounds = true; tagsLine.translatesAutoresizingMaskIntoConstraints = false
        let usage = NSTextField(labelWithString: status); usage.font = Theme.caption(); usage.textColor = .tertiaryLabelColor; usage.translatesAutoresizingMaskIntoConstraints = false
        let noteState = NSTextField(labelWithString: hasCreatorNote ? "✎ 有随手记" : "待记录"); noteState.font = Theme.caption(); noteState.textColor = hasCreatorNote ? .systemGreen : .systemOrange; noteState.alignment = .right; noteState.translatesAutoresizingMaskIntoConstraints = false
        let handle = NSTextField(labelWithString: "⠿"); handle.font = .systemFont(ofSize: 11); handle.textColor = .tertiaryLabelColor; handle.translatesAutoresizingMaskIntoConstraints = false
        let separator = NSBox(); separator.boxType = .separator; separator.translatesAutoresizingMaskIntoConstraints = false
        addSubview(image); addSubview(text); addSubview(tagsLine); addSubview(usage); addSubview(noteState); addSubview(handle); addSubview(separator)
        NSLayoutConstraint.activate([heightAnchor.constraint(equalToConstant: 78), image.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4), image.centerYAnchor.constraint(equalTo: centerYAnchor), image.widthAnchor.constraint(equalToConstant: 82), image.heightAnchor.constraint(equalToConstant: 46), text.leadingAnchor.constraint(equalTo: image.trailingAnchor, constant: 8), text.trailingAnchor.constraint(equalTo: handle.leadingAnchor, constant: -3), text.topAnchor.constraint(equalTo: topAnchor, constant: 6), tagsLine.leadingAnchor.constraint(equalTo: text.leadingAnchor), tagsLine.trailingAnchor.constraint(equalTo: handle.leadingAnchor, constant: -3), tagsLine.topAnchor.constraint(equalTo: text.bottomAnchor, constant: 2), usage.leadingAnchor.constraint(equalTo: text.leadingAnchor), usage.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -6), noteState.trailingAnchor.constraint(equalTo: handle.leadingAnchor, constant: -4), noteState.bottomAnchor.constraint(equalTo: usage.bottomAnchor), noteState.leadingAnchor.constraint(greaterThanOrEqualTo: usage.trailingAnchor, constant: 5), handle.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6), handle.centerYAnchor.constraint(equalTo: centerYAnchor), separator.leadingAnchor.constraint(equalTo: text.leadingAnchor), separator.trailingAnchor.constraint(equalTo: trailingAnchor), separator.bottomAnchor.constraint(equalTo: bottomAnchor)])
    }
    required init?(coder: NSCoder) { nil }
    override func mouseDown(with event: NSEvent) { mouseDownPoint = convert(event.locationInWindow, from: nil); dragStarted = false }
    override func mouseDragged(with event: NSEvent) { guard !dragStarted else { return }; let point = convert(event.locationInWindow, from: nil); guard hypot(point.x - mouseDownPoint.x, point.y - mouseDownPoint.y) > 4 else { return }; dragStarted = true; let item = NSPasteboardItem(); item.setString(String(clipID), forType: clipPasteboardType); let drag = NSDraggingItem(pasteboardWriter: item); drag.setDraggingFrame(bounds, contents: reelsiftDragImage()); beginDraggingSession(with: [drag], event: event, source: self) }
    override func mouseUp(with event: NSEvent) { if !dragStarted { selectAction() } }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .move }
}

final class BeatClipCardView: NSView, NSDraggingSource {
    private let clipID: Int; private let selectAction: () -> Void; private var mouseDownPoint: NSPoint = .zero; private var dragStarted = false
    init(clip: DeskClip, selected: Bool, select: @escaping () -> Void) {
        clipID = clip.id; selectAction = select; super.init(frame: .zero); wantsLayer = true
        layer?.backgroundColor = selected ? Theme.brandLime.withAlphaComponent(0.16).cgColor : NSColor.textBackgroundColor.cgColor
        layer?.cornerRadius = Theme.radiusCard; layer?.borderWidth = selected ? 1 : 0.5; layer?.borderColor = selected ? Theme.brandLime.cgColor : NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha).cgColor
        let image = NSImageView(); image.imageScaling = .scaleProportionallyUpOrDown; image.image = clip.coverPath.isEmpty ? nil : NSImage(contentsOfFile: clip.coverPath); image.wantsLayer = true; image.layer?.backgroundColor = NSColor.tertiaryLabelColor.withAlphaComponent(0.10).cgColor; image.layer?.masksToBounds = true; image.layer?.cornerRadius = 4; image.translatesAutoresizingMaskIntoConstraints = false
        let name = NSTextField(labelWithString: clip.filename); name.font = Theme.caption(); name.lineBreakMode = .byTruncatingMiddle; name.translatesAutoresizingMaskIntoConstraints = false
        let handle = NSTextField(labelWithString: "⠿"); handle.font = .systemFont(ofSize: 9); handle.textColor = .tertiaryLabelColor; handle.translatesAutoresizingMaskIntoConstraints = false
        addSubview(image); addSubview(name); addSubview(handle)
        NSLayoutConstraint.activate([widthAnchor.constraint(equalToConstant: 132), heightAnchor.constraint(equalToConstant: 94), image.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 5), image.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -5), image.topAnchor.constraint(equalTo: topAnchor, constant: 5), image.heightAnchor.constraint(equalToConstant: 68), name.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 7), name.trailingAnchor.constraint(equalTo: handle.leadingAnchor, constant: -2), name.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -7), handle.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -7), handle.centerYAnchor.constraint(equalTo: name.centerYAnchor)])
    }
    required init?(coder: NSCoder) { nil }
    override func mouseDown(with event: NSEvent) { mouseDownPoint = convert(event.locationInWindow, from: nil); dragStarted = false }
    override func mouseDragged(with event: NSEvent) { guard !dragStarted else { return }; let point = convert(event.locationInWindow, from: nil); guard hypot(point.x - mouseDownPoint.x, point.y - mouseDownPoint.y) > 4 else { return }; dragStarted = true; let item = NSPasteboardItem(); item.setString(String(clipID), forType: clipPasteboardType); let drag = NSDraggingItem(pasteboardWriter: item); drag.setDraggingFrame(bounds, contents: reelsiftDragImage()); beginDraggingSession(with: [drag], event: event, source: self) }
    override func mouseUp(with event: NSEvent) { if !dragStarted { selectAction() } }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .move }
}

final class BeatHandleView: NSTextField, NSDraggingSource {
    let beatID: Int; private let selectAction: () -> Void; private var mouseDownPoint: NSPoint = .zero; private var dragStarted = false
    init(index: Int, beatID: Int, select: @escaping () -> Void) { self.beatID = beatID; selectAction = select; super.init(frame: .zero); stringValue = "⠿  " + String(format: "%02d", index); isEditable = false; isBordered = false; drawsBackground = false; font = .monospacedDigitSystemFont(ofSize: 12, weight: .semibold); textColor = .secondaryLabelColor; alignment = .left }
    required init?(coder: NSCoder) { nil }
    override func mouseDown(with event: NSEvent) { mouseDownPoint = convert(event.locationInWindow, from: nil); dragStarted = false }
    override func mouseDragged(with event: NSEvent) { guard !dragStarted else { return }; let point = convert(event.locationInWindow, from: nil); guard hypot(point.x - mouseDownPoint.x, point.y - mouseDownPoint.y) > 4 else { return }; dragStarted = true; let item = NSPasteboardItem(); item.setString(String(beatID), forType: beatPasteboardType); let drag = NSDraggingItem(pasteboardWriter: item); drag.setDraggingFrame(bounds, contents: reelsiftDragImage()); beginDraggingSession(with: [drag], event: event, source: self) }
    override func mouseUp(with event: NSEvent) { if !dragStarted { selectAction() } }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .move }
}

final class DropZoneView: NSView {
    var onClip: ((Int) -> Void)?; var onBeat: ((Int) -> Void)?
    private var baseBorderWidth: CGFloat = 0.5
    private var baseCornerRadius: CGFloat = 4
    private var baseBackgroundColor: NSColor = .clear
    init(label: String) { super.init(frame: .zero); wantsLayer = true; layer?.borderColor = NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha).cgColor; layer?.borderWidth = 0.5; layer?.cornerRadius = Theme.radiusChip; toolTip = label.isEmpty ? "Drop footage or a Story Beat here" : label; if !label.isEmpty { let text = NSTextField(labelWithString: label); text.font = Theme.small(); text.textColor = .secondaryLabelColor; text.translatesAutoresizingMaskIntoConstraints = false; addSubview(text); NSLayoutConstraint.activate([text.centerXAnchor.constraint(equalTo: centerXAnchor), text.centerYAnchor.constraint(equalTo: centerYAnchor)]) }; registerForDraggedTypes([clipPasteboardType, beatPasteboardType]) }
    required init?(coder: NSCoder) { nil }
    func setBaseStyle(borderWidth: CGFloat, cornerRadius: CGFloat, backgroundColor: NSColor) { baseBorderWidth = borderWidth; baseCornerRadius = cornerRadius; baseBackgroundColor = backgroundColor; restoreAppearance() }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { layer?.borderColor = Theme.brandLime.cgColor; layer?.borderWidth = 1.5; layer?.backgroundColor = Theme.brandLime.withAlphaComponent(0.16).cgColor; return .move }
    override func draggingExited(_ sender: NSDraggingInfo?) { restoreAppearance() }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool { defer { restoreAppearance() }; let board = sender.draggingPasteboard; if let v = board.string(forType: clipPasteboardType), let id = Int(v) { onClip?(id); return true }; if let v = board.string(forType: beatPasteboardType), let id = Int(v) { onBeat?(id); return true }; return false }
    private func restoreAppearance() { layer?.borderColor = NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha).cgColor; layer?.borderWidth = baseBorderWidth; layer?.cornerRadius = baseCornerRadius; layer?.backgroundColor = baseBackgroundColor.cgColor }
}

struct BridgeEnvelope: Decodable {
    let ok: Bool?
    let payload: DeskPayload?
    let error: String?
    let aiError: String?

    enum CodingKeys: String, CodingKey {
        case ok, payload, error
        case aiError = "ai_error"
    }
}

private enum NoteTarget: Hashable {
    case folder(Int)
    case clip(Int)
    case beat(Int)

    var displayName: String {
        switch self {
        case .folder: return "项目随手记"
        case .clip: return "素材随手记"
        case .beat: return "Beat 随手记"
        }
    }
}

final class DirectorDeskController: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextViewDelegate, NSTextFieldDelegate, NSWindowDelegate {
    private let folder: URL; private var payload: DeskPayload?; private var filter = "all"; private var selectedClipID: Int?; private var selectedBeatID: Int?
    private let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1420, height: 900), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
    private let footageTable = NSTableView(); private let canvasStack = NSStackView(); private let canvasScroll = NSScrollView(); private let inspectorStack = NSStackView(); private let inspectorScroll = NSScrollView(); private let mainSplit = NSSplitView(); private weak var inspectorPane: NSView?; private let player = AVPlayerView(); private let reviewPlayer = AVPlayerView(); private let statusLabel = NSTextField(labelWithString: "读取本地 Story 状态…"); private var fieldBindings: [ObjectIdentifier: (Int, String)] = [:]; private let folderNote = PlaceholderTextView(); private var noteBindings: [ObjectIdentifier: NoteTarget] = [:]; private var reviewNoteBindings: [ObjectIdentifier: Int] = [:]; private var noteStateLabels: [ObjectIdentifier: NSTextField] = [:]; private var noteSaveTimer: Timer?; private var pendingNoteTarget: NoteTarget?; private var pendingNoteText = ""; private var clipNoteDrafts: [Int: String] = [:]; private var beatNoteDrafts: [Int: String] = [:]; private var reviewStatusDrafts: [Int: String] = [:]; private var previewQueue: AVQueuePlayer?; private var previewRanges: [(Double?, Double?)] = []; private var previewLabels: [(beat: String, clip: String)] = []; private var previewRangeIndex = 0; private weak var previewActiveItem: AVPlayerItem?; private var previewObserver: Any?; private var previewItemObservation: NSKeyValueObservation?
    private let projectTitleLabel = NSTextField(labelWithString: "")
    private let projectMetaLabel = NSTextField(labelWithString: "读取项目中…")
    private var navigationButtons: [String: NSButton] = [:]
    private var navigationNameLabels: [String: NSTextField] = [:]
    private var navigationCountLabels: [String: NSTextField] = [:]
    private let footagePoolCountLabel = NSTextField(labelWithString: "")
    private var inspectorMode = "story"
    private var workflowStep = 1
    private var flowButtons: [Int: NSButton] = [:]
    private var flowTitles: [Int: NSTextField] = [:]
    private var flowSubtitles: [Int: NSTextField] = [:]
    private var storyDirections: [StoryDirection] = []
    private var selectedDirectionStoryID: Int?
    private var storyGenerationBusy = false
    private var pendingDirectionTitle = ""
    private let storyBriefField = PlaceholderTextView()
    private var directionChatMessages: [(role: String, text: String)] = []
    private var directionsBusy = false
    private let generateDirectionsButton = NSButton()
    private let generateDirectionsSpinner = NSProgressIndicator()
    private var storySuggestionError = ""
    private var storyChatMessages: [StoryChatMessage] = []
    private var storyChatLoadedForStoryID: Int?
    private var storyChatBusy = false
    private var storyMoveRevision = 0
    private var latestDragSuggestion: StorySuggestion?
    private var hiddenSuggestionIDs: Set<Int> = []
    private var expandedReasoningMessageIDs: Set<Int> = []
    private var resolvedSuggestionIDs: Set<Int> = []
    private let storyChatInput = NSTextField()
    private let storyChatSendButton = NSButton()
    private let storyChatSpinner = NSProgressIndicator()
    init(folder: URL) { self.folder = folder; super.init(); buildWindow(); loadData() }
    func show() { window.collectionBehavior.insert(.moveToActiveSpace); window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }

    private func buildWindow() {
        window.title = "Reelsift · Visual Story Editor"; window.delegate = self
        let root = NSView(); root.wantsLayer = true; root.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor; window.contentView = root
        let header = NSView(); header.translatesAutoresizingMaskIntoConstraints = false; header.wantsLayer = true; header.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor; root.addSubview(header)
        let brandMark = makeCenteredBadge("R", font: .systemFont(ofSize: 10, weight: .black), textColor: Theme.brandForest, fill: Theme.brandLime, size: NSSize(width: 22, height: 22), cornerRadius: Theme.radiusChip)
        let title = NSTextField(labelWithString: "Reelsift"); title.font = Theme.title(); title.translatesAutoresizingMaskIntoConstraints = false
        let subtitle = NSTextField(labelWithString: "视觉故事编辑器"); subtitle.font = Theme.body(); subtitle.textColor = .secondaryLabelColor; subtitle.translatesAutoresizingMaskIntoConstraints = false
        statusLabel.font = Theme.body(); statusLabel.textColor = .secondaryLabelColor; statusLabel.translatesAutoresizingMaskIntoConstraints = false
        let flowBar = NSView(); flowBar.wantsLayer = true; flowBar.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor; flowBar.translatesAutoresizingMaskIntoConstraints = false
        header.addSubview(brandMark); header.addSubview(title); header.addSubview(subtitle); header.addSubview(statusLabel); header.addSubview(flowBar)
        NSLayoutConstraint.activate([header.leadingAnchor.constraint(equalTo: root.leadingAnchor), header.trailingAnchor.constraint(equalTo: root.trailingAnchor), header.topAnchor.constraint(equalTo: root.topAnchor), header.heightAnchor.constraint(equalToConstant: 118), brandMark.leadingAnchor.constraint(equalTo: header.leadingAnchor, constant: Theme.space4), brandMark.centerYAnchor.constraint(equalTo: title.centerYAnchor), title.leadingAnchor.constraint(equalTo: brandMark.trailingAnchor, constant: Theme.space2), title.topAnchor.constraint(equalTo: header.topAnchor, constant: Theme.space4), subtitle.leadingAnchor.constraint(equalTo: title.trailingAnchor, constant: Theme.space2), subtitle.centerYAnchor.constraint(equalTo: title.centerYAnchor), statusLabel.trailingAnchor.constraint(equalTo: header.trailingAnchor, constant: -Theme.space4), statusLabel.centerYAnchor.constraint(equalTo: title.centerYAnchor), flowBar.leadingAnchor.constraint(equalTo: header.leadingAnchor), flowBar.trailingAnchor.constraint(equalTo: header.trailingAnchor), flowBar.bottomAnchor.constraint(equalTo: header.bottomAnchor), flowBar.heightAnchor.constraint(equalToConstant: 58)])
        let steps: [(Int, String, String)] = [(1, "查看素材", "查看素材 / 补充随手记"), (2, "生成故事", "基于素材与随手记生成"), (3, "编排故事", "拖动素材 / 调整故事段落"), (4, "预览故事", "预览当前故事")]
        var previous: NSButton?
        for (step, name, detail) in steps {
            let button = NSButton(title: "", target: self, action: #selector(changeWorkflowStep(_:))); button.tag = step; button.isBordered = false; button.wantsLayer = true; button.layer?.cornerRadius = Theme.radiusChip; button.translatesAutoresizingMaskIntoConstraints = false
            let badge = NSView(); badge.wantsLayer = true; badge.layer?.cornerRadius = 13; badge.layer?.masksToBounds = true; badge.translatesAutoresizingMaskIntoConstraints = false
            let number = NSTextField(labelWithString: String(step)); number.tag = 100 + step; number.font = .monospacedDigitSystemFont(ofSize: 11, weight: .bold); number.alignment = .center; number.translatesAutoresizingMaskIntoConstraints = false
            let nameLabel = NSTextField(labelWithString: name); nameLabel.font = Theme.body(); nameLabel.translatesAutoresizingMaskIntoConstraints = false
            let detailLabel = NSTextField(labelWithString: detail); detailLabel.font = Theme.caption(); detailLabel.textColor = .secondaryLabelColor; detailLabel.translatesAutoresizingMaskIntoConstraints = false
            badge.addSubview(number); button.addSubview(badge); button.addSubview(nameLabel); button.addSubview(detailLabel); flowBar.addSubview(button)
            NSLayoutConstraint.activate([button.topAnchor.constraint(equalTo: flowBar.topAnchor, constant: 6), button.bottomAnchor.constraint(equalTo: flowBar.bottomAnchor, constant: -6), badge.leadingAnchor.constraint(equalTo: button.leadingAnchor, constant: 12), badge.topAnchor.constraint(equalTo: button.topAnchor, constant: 7), badge.widthAnchor.constraint(equalToConstant: 26), badge.heightAnchor.constraint(equalToConstant: 26), number.centerXAnchor.constraint(equalTo: badge.centerXAnchor), number.centerYAnchor.constraint(equalTo: badge.centerYAnchor), nameLabel.leadingAnchor.constraint(equalTo: badge.trailingAnchor, constant: 8), nameLabel.topAnchor.constraint(equalTo: button.topAnchor, constant: 7), detailLabel.leadingAnchor.constraint(equalTo: nameLabel.leadingAnchor), detailLabel.topAnchor.constraint(equalTo: nameLabel.bottomAnchor, constant: 2), detailLabel.trailingAnchor.constraint(equalTo: button.trailingAnchor, constant: -8)])
            if let previous { button.leadingAnchor.constraint(equalTo: previous.trailingAnchor, constant: 4).isActive = true; button.widthAnchor.constraint(equalTo: previous.widthAnchor).isActive = true } else { button.leadingAnchor.constraint(equalTo: flowBar.leadingAnchor, constant: 14).isActive = true }
            if step == 4 { button.trailingAnchor.constraint(equalTo: flowBar.trailingAnchor, constant: -14).isActive = true }
            previous = button; flowButtons[step] = button; flowTitles[step] = nameLabel; flowSubtitles[step] = detailLabel
        }
        updateFlowStepper()
        mainSplit.isVertical = true; mainSplit.dividerStyle = .thin; mainSplit.translatesAutoresizingMaskIntoConstraints = false; root.addSubview(mainSplit); NSLayoutConstraint.activate([mainSplit.leadingAnchor.constraint(equalTo: root.leadingAnchor), mainSplit.trailingAnchor.constraint(equalTo: root.trailingAnchor), mainSplit.topAnchor.constraint(equalTo: header.bottomAnchor), mainSplit.bottomAnchor.constraint(equalTo: root.bottomAnchor)]); mainSplit.addSubview(buildFootagePane()); mainSplit.addSubview(buildCanvasPane()); mainSplit.addSubview(buildInspectorPane())
    }

    private func updateFlowStepper() {
        let labels = [1: "查看素材 · 随手记可选", 2: "生成故事 · 素材 + 随手记", 3: "编排故事 · 直接调整素材与段落", 4: "预览故事 · 验证当前故事"]
        statusLabel.stringValue = labels[workflowStep] ?? ""
        for step in 1...4 {
            guard let button = flowButtons[step], let title = flowTitles[step], let subtitle = flowSubtitles[step], let number = button.viewWithTag(100 + step) as? NSTextField else { continue }
            let active = step == workflowStep; let done = step < workflowStep
            button.layer?.backgroundColor = active ? Theme.brandLime.withAlphaComponent(0.16).cgColor : NSColor.clear.cgColor
            title.textColor = active ? Theme.brandForest : .labelColor; subtitle.textColor = active ? Theme.brandMoss : .secondaryLabelColor
            number.stringValue = done ? "✓" : String(step); number.textColor = (active || done) ? Theme.brandForest : .secondaryLabelColor; number.superview?.layer?.backgroundColor = (active || done) ? Theme.brandLime.cgColor : NSColor.controlBackgroundColor.cgColor
        }
        inspectorPane?.isHidden = workflowStep == 4
        mainSplit.adjustSubviews()
    }
    private func buildFootagePane() -> NSView {
        let pane = NSView(); pane.wantsLayer = true; pane.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        let projectCard = NSView(); Theme.applyCardStyle(projectCard, cornerRadius: Theme.radiusCard); projectCard.translatesAutoresizingMaskIntoConstraints = false
        let projectIcon = NSImageView(); projectIcon.image = NSWorkspace.shared.icon(forFile: folder.path); projectIcon.imageScaling = .scaleProportionallyUpOrDown; projectIcon.translatesAutoresizingMaskIntoConstraints = false
        projectTitleLabel.stringValue = folder.lastPathComponent; projectTitleLabel.font = .systemFont(ofSize: 13, weight: .semibold); projectTitleLabel.lineBreakMode = .byTruncatingMiddle; projectTitleLabel.translatesAutoresizingMaskIntoConstraints = false
        projectMetaLabel.font = .systemFont(ofSize: 11); projectMetaLabel.textColor = .secondaryLabelColor; projectMetaLabel.translatesAutoresizingMaskIntoConstraints = false
        let chevron = NSTextField(labelWithString: "⌄"); chevron.font = .systemFont(ofSize: 14, weight: .medium); chevron.textColor = .tertiaryLabelColor; chevron.translatesAutoresizingMaskIntoConstraints = false
        projectCard.addSubview(projectIcon); projectCard.addSubview(projectTitleLabel); projectCard.addSubview(projectMetaLabel); projectCard.addSubview(chevron)
        NSLayoutConstraint.activate([projectCard.heightAnchor.constraint(equalToConstant: 66), projectIcon.leadingAnchor.constraint(equalTo: projectCard.leadingAnchor, constant: 10), projectIcon.centerYAnchor.constraint(equalTo: projectCard.centerYAnchor), projectIcon.widthAnchor.constraint(equalToConstant: 38), projectIcon.heightAnchor.constraint(equalToConstant: 38), projectTitleLabel.leadingAnchor.constraint(equalTo: projectIcon.trailingAnchor, constant: 8), projectTitleLabel.trailingAnchor.constraint(equalTo: chevron.leadingAnchor, constant: -5), projectTitleLabel.topAnchor.constraint(equalTo: projectCard.topAnchor, constant: 13), projectMetaLabel.leadingAnchor.constraint(equalTo: projectTitleLabel.leadingAnchor), projectMetaLabel.topAnchor.constraint(equalTo: projectTitleLabel.bottomAnchor, constant: 3), chevron.trailingAnchor.constraint(equalTo: projectCard.trailingAnchor, constant: -10), chevron.centerYAnchor.constraint(equalTo: projectCard.centerYAnchor)])
        let nav = NSStackView(); nav.orientation = .vertical; nav.spacing = 2; nav.translatesAutoresizingMaskIntoConstraints = false
        [("all", "▣", "全部素材"), ("reviewed", "✓", "已查看"), ("notes", "✎", "有随手记")].forEach { id, icon, name in
            let button = NSButton(title: "", target: self, action: #selector(changeFilter(_:))); button.identifier = NSUserInterfaceItemIdentifier(id); button.isBordered = false; button.wantsLayer = true; button.layer?.cornerRadius = Theme.radiusChip; button.heightAnchor.constraint(equalToConstant: 30).isActive = true; button.toolTip = name
            let iconLabel = NSTextField(labelWithString: icon); iconLabel.font = .systemFont(ofSize: 13, weight: .medium); iconLabel.translatesAutoresizingMaskIntoConstraints = false
            let nameLabel = NSTextField(labelWithString: name); nameLabel.font = .systemFont(ofSize: 12.5); nameLabel.translatesAutoresizingMaskIntoConstraints = false
            let countLabel = NSTextField(labelWithString: "0"); countLabel.font = .monospacedDigitSystemFont(ofSize: 11, weight: .regular); countLabel.textColor = .tertiaryLabelColor; countLabel.alignment = .right; countLabel.translatesAutoresizingMaskIntoConstraints = false
            button.addSubview(iconLabel); button.addSubview(nameLabel); button.addSubview(countLabel)
            NSLayoutConstraint.activate([iconLabel.leadingAnchor.constraint(equalTo: button.leadingAnchor, constant: 9), iconLabel.centerYAnchor.constraint(equalTo: button.centerYAnchor), nameLabel.leadingAnchor.constraint(equalTo: iconLabel.trailingAnchor, constant: 8), nameLabel.centerYAnchor.constraint(equalTo: button.centerYAnchor), countLabel.trailingAnchor.constraint(equalTo: button.trailingAnchor, constant: -9), countLabel.centerYAnchor.constraint(equalTo: button.centerYAnchor), countLabel.leadingAnchor.constraint(greaterThanOrEqualTo: nameLabel.trailingAnchor, constant: 8)])
            navigationButtons[id] = button; navigationNameLabels[id] = nameLabel; navigationCountLabels[id] = countLabel; nav.addArrangedSubview(button)
        }
        let label = NSTextField(labelWithString: "素材库"); label.font = .systemFont(ofSize: 10, weight: .semibold); label.textColor = .secondaryLabelColor; label.translatesAutoresizingMaskIntoConstraints = false
        footagePoolCountLabel.font = .monospacedDigitSystemFont(ofSize: 10, weight: .regular); footagePoolCountLabel.textColor = .tertiaryLabelColor; footagePoolCountLabel.alignment = .right; footagePoolCountLabel.translatesAutoresizingMaskIntoConstraints = false
        let scroll = NSScrollView(); scroll.hasVerticalScroller = true; scroll.drawsBackground = false; scroll.translatesAutoresizingMaskIntoConstraints = false; footageTable.addTableColumn(NSTableColumn(identifier: NSUserInterfaceItemIdentifier("clip"))); footageTable.headerView = nil; footageTable.rowHeight = 78; footageTable.intercellSpacing = NSSize(width: 0, height: 1); footageTable.delegate = self; footageTable.dataSource = self; scroll.documentView = footageTable
        pane.addSubview(projectCard); pane.addSubview(nav); pane.addSubview(label); pane.addSubview(footagePoolCountLabel); pane.addSubview(scroll)
        NSLayoutConstraint.activate([pane.widthAnchor.constraint(equalToConstant: 250), projectCard.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 12), projectCard.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -12), projectCard.topAnchor.constraint(equalTo: pane.topAnchor, constant: 14), nav.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 10), nav.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -10), nav.topAnchor.constraint(equalTo: projectCard.bottomAnchor, constant: 12), label.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 14), label.topAnchor.constraint(equalTo: nav.bottomAnchor, constant: 16), footagePoolCountLabel.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -14), footagePoolCountLabel.centerYAnchor.constraint(equalTo: label.centerYAnchor), scroll.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 8), scroll.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -8), scroll.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 6), scroll.bottomAnchor.constraint(equalTo: pane.bottomAnchor, constant: -8)])
        return pane
    }
    private func buildCanvasPane() -> NSView { let pane = NSView(); pane.wantsLayer = true; pane.layer?.backgroundColor = NSColor.textBackgroundColor.cgColor; canvasScroll.hasVerticalScroller = true; canvasScroll.drawsBackground = true; canvasScroll.backgroundColor = .textBackgroundColor; canvasScroll.translatesAutoresizingMaskIntoConstraints = false; let document = FlippedView(); document.translatesAutoresizingMaskIntoConstraints = false; canvasScroll.documentView = document; canvasStack.orientation = .vertical; canvasStack.alignment = .leading; canvasStack.spacing = 8; canvasStack.translatesAutoresizingMaskIntoConstraints = false; document.addSubview(canvasStack); pane.addSubview(canvasScroll); NSLayoutConstraint.activate([pane.widthAnchor.constraint(greaterThanOrEqualToConstant: 650), canvasScroll.leadingAnchor.constraint(equalTo: pane.leadingAnchor), canvasScroll.trailingAnchor.constraint(equalTo: pane.trailingAnchor), canvasScroll.topAnchor.constraint(equalTo: pane.topAnchor), canvasScroll.bottomAnchor.constraint(equalTo: pane.bottomAnchor), document.widthAnchor.constraint(equalTo: canvasScroll.contentView.widthAnchor), canvasStack.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 18), canvasStack.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -18), canvasStack.topAnchor.constraint(equalTo: document.topAnchor, constant: 16), canvasStack.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -22)]); return pane }
    private func buildInspectorPane() -> NSView {
        let pane = NSView(); inspectorPane = pane; pane.wantsLayer = true; pane.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        inspectorScroll.hasVerticalScroller = true; inspectorScroll.drawsBackground = false; inspectorScroll.translatesAutoresizingMaskIntoConstraints = false
        let document = FlippedView(); document.translatesAutoresizingMaskIntoConstraints = false; inspectorScroll.documentView = document
        inspectorStack.orientation = .vertical; inspectorStack.alignment = .width; inspectorStack.spacing = 10; inspectorStack.translatesAutoresizingMaskIntoConstraints = false
        player.controlsStyle = .floating; player.wantsLayer = true; player.layer?.backgroundColor = NSColor.black.cgColor; player.layer?.cornerRadius = 4; player.layer?.masksToBounds = true; player.translatesAutoresizingMaskIntoConstraints = false; player.heightAnchor.constraint(equalTo: player.widthAnchor, multiplier: 9.0 / 16.0).isActive = true
        document.addSubview(inspectorStack); pane.addSubview(inspectorScroll)
        NSLayoutConstraint.activate([pane.widthAnchor.constraint(equalToConstant: 350), inspectorScroll.leadingAnchor.constraint(equalTo: pane.leadingAnchor, constant: 14), inspectorScroll.trailingAnchor.constraint(equalTo: pane.trailingAnchor, constant: -14), inspectorScroll.topAnchor.constraint(equalTo: pane.topAnchor, constant: 14), inspectorScroll.bottomAnchor.constraint(equalTo: pane.bottomAnchor, constant: -14), document.widthAnchor.constraint(equalTo: inspectorScroll.contentView.widthAnchor), inspectorStack.leadingAnchor.constraint(equalTo: document.leadingAnchor), inspectorStack.trailingAnchor.constraint(equalTo: document.trailingAnchor), inspectorStack.topAnchor.constraint(equalTo: document.topAnchor), inspectorStack.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -18)])
        return pane
    }
    private func divider() -> NSBox { let box = NSBox(); box.boxType = .separator; return box }
    private func makeFolderNote() -> NSView {
        let wrap = NSView(); Theme.applyCardStyle(wrap, cornerRadius: Theme.radiusCard)
        let heading = NSTextField(labelWithString: "FOLDER NOTE"); heading.font = .systemFont(ofSize: 10, weight: .semibold); heading.textColor = .secondaryLabelColor; heading.translatesAutoresizingMaskIntoConstraints = false
        let subheading = NSTextField(labelWithString: "项目随手记 · 输入停止后自动保存"); subheading.font = .systemFont(ofSize: 10.5); subheading.textColor = .tertiaryLabelColor; subheading.translatesAutoresizingMaskIntoConstraints = false
        let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.translatesAutoresizingMaskIntoConstraints = false
        folderNote.font = .systemFont(ofSize: 12); folderNote.textColor = .labelColor; folderNote.isRichText = false; folderNote.drawsBackground = false; folderNote.textContainerInset = NSSize(width: 1, height: 3); folderNote.textContainer?.widthTracksTextView = true; folderNote.placeholderString = "记录这个项目的主题、创作意图或之后要补的镜头…"; if let payload { bindNoteTextView(folderNote, target: .folder(payload.libraryID)) }; scroll.documentView = folderNote
        wrap.addSubview(heading); wrap.addSubview(subheading); wrap.addSubview(scroll)
        NSLayoutConstraint.activate([wrap.heightAnchor.constraint(equalToConstant: 122), heading.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 12), heading.topAnchor.constraint(equalTo: wrap.topAnchor, constant: 11), subheading.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -12), subheading.centerYAnchor.constraint(equalTo: heading.centerYAnchor), scroll.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 11), scroll.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -11), scroll.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 5), scroll.bottomAnchor.constraint(equalTo: wrap.bottomAnchor, constant: -8)])
        return wrap
    }

    private func displayedClipNote(_ clip: DeskClip) -> String { clipNoteDrafts[clip.id] ?? clip.userNote }
    private func displayedBeatNote(_ beat: StoryBeat) -> String { beatNoteDrafts[beat.id] ?? beat.userNote }
    private func hasCreatorNote(_ clip: DeskClip) -> Bool { !displayedClipNote(clip).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func bindNoteTextView(_ textView: PlaceholderTextView, target: NoteTarget, stateLabel: NSTextField? = nil) {
        textView.delegate = self
        noteBindings[ObjectIdentifier(textView)] = target
        if let stateLabel { noteStateLabels[ObjectIdentifier(textView)] = stateLabel }
    }

    private func makeNoteEditor(title: String, caption: String, value: String, placeholder: String, target: NoteTarget, height: CGFloat = 118) -> NSView {
        let wrap = NSView(); Theme.applyCardStyle(wrap, cornerRadius: Theme.radiusCard)
        let heading = NSTextField(labelWithString: title); heading.font = .systemFont(ofSize: 10, weight: .semibold); heading.textColor = .secondaryLabelColor; heading.translatesAutoresizingMaskIntoConstraints = false
        let state = NSTextField(labelWithString: "自动保存"); state.font = .systemFont(ofSize: 10); state.textColor = .tertiaryLabelColor; state.alignment = .right; state.translatesAutoresizingMaskIntoConstraints = false
        let textView = PlaceholderTextView(); textView.string = value; textView.placeholderString = placeholder; textView.font = .systemFont(ofSize: 12); textView.textColor = .labelColor; textView.isRichText = false; textView.drawsBackground = false; textView.textContainerInset = NSSize(width: 1, height: 3); textView.textContainer?.widthTracksTextView = true; bindNoteTextView(textView, target: target, stateLabel: state)
        let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.translatesAutoresizingMaskIntoConstraints = false; scroll.documentView = textView
        let hint = NSTextField(labelWithString: caption); hint.font = .systemFont(ofSize: 10.5); hint.textColor = .tertiaryLabelColor; hint.lineBreakMode = .byTruncatingTail; hint.translatesAutoresizingMaskIntoConstraints = false
        wrap.addSubview(heading); wrap.addSubview(state); wrap.addSubview(scroll); wrap.addSubview(hint)
        NSLayoutConstraint.activate([wrap.heightAnchor.constraint(equalToConstant: height), heading.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 11), heading.topAnchor.constraint(equalTo: wrap.topAnchor, constant: 10), state.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -11), state.centerYAnchor.constraint(equalTo: heading.centerYAnchor), state.leadingAnchor.constraint(greaterThanOrEqualTo: heading.trailingAnchor, constant: 8), scroll.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 10), scroll.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -10), scroll.topAnchor.constraint(equalTo: heading.bottomAnchor, constant: 4), scroll.bottomAnchor.constraint(equalTo: hint.topAnchor, constant: -3), hint.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 11), hint.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -11), hint.bottomAnchor.constraint(equalTo: wrap.bottomAnchor, constant: -8)])
        return wrap
    }

    private func loadData(finalStatus: String? = nil) { runBridge(["load", folder.path], label: "正在读取已识别素材…", completion: { [weak self] data in guard let self else { return }; guard let decoded = self.decodePayload(data) else { self.showBridgeError("本地 Story 数据格式无法读取"); return }; self.payload = decoded; if self.selectedDirectionStoryID == nil { self.selectedDirectionStoryID = decoded.story?.id }; self.folderNote.string = decoded.memoryNote; self.rebuildInterface(); if let finalStatus { self.statusLabel.stringValue = finalStatus } else if !decoded.restoreWarning.isEmpty { self.statusLabel.stringValue = decoded.restoreWarning } }) }
    private func runBridge(_ args: [String], label: String, onBusyChange: ((Bool) -> Void)? = nil, completion: @escaping (Data) -> Void = { _ in }) {
        statusLabel.stringValue = label
        onBusyChange?(true)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: pythonPath)
            process.arguments = [bridgePath] + args
            process.currentDirectoryURL = URL(fileURLWithPath: projectDirectory)
            let token = UUID().uuidString
            let outputURL = FileManager.default.temporaryDirectory.appendingPathComponent("reelsift-\(token).json")
            let errorURL = FileManager.default.temporaryDirectory.appendingPathComponent("reelsift-\(token).err")
            FileManager.default.createFile(atPath: outputURL.path, contents: nil)
            FileManager.default.createFile(atPath: errorURL.path, contents: nil)
            defer {
                try? FileManager.default.removeItem(at: outputURL)
                try? FileManager.default.removeItem(at: errorURL)
            }
            do {
                let outputHandle = try FileHandle(forWritingTo: outputURL)
                let errorHandle = try FileHandle(forWritingTo: errorURL)
                process.standardOutput = outputHandle
                process.standardError = errorHandle
                try process.run()
                process.waitUntilExit()
                try? outputHandle.close()
                try? errorHandle.close()
                let data = (try? Data(contentsOf: outputURL)) ?? Data()
                let error = (try? String(contentsOf: errorURL, encoding: .utf8)) ?? "本地 bridge 调用失败"
                DispatchQueue.main.async {
                    defer { onBusyChange?(false) }
                    if process.terminationStatus != 0 {
                        self.showBridgeError(error.trimmingCharacters(in: .whitespacesAndNewlines))
                        return
                    }
                    self.statusLabel.stringValue = "已保存到本地数据库"
                    completion(data)
                }
            } catch {
                DispatchQueue.main.async { onBusyChange?(false); self.showBridgeError(error.localizedDescription) }
            }
        }
    }
    private func decodePayload(_ data: Data) -> DeskPayload? {
        if let envelope = try? JSONDecoder().decode(BridgeEnvelope.self, from: data), let payload = envelope.payload { return payload }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any], object["folder"] != nil else { return nil }
        return try? JSONDecoder().decode(DeskPayload.self, from: data)
    }
    private func applyBridgePayload(_ data: Data) {
        if let envelope = try? JSONDecoder().decode(BridgeEnvelope.self, from: data) {
            if envelope.ok == false {
                showBridgeError(envelope.error ?? "本地操作没有完成，请重试。")
                return
            }
            if let decoded = envelope.payload {
                payload = decoded
                if decoded.pendingSuggestion != nil { storySuggestionError = "" }
                folderNote.string = decoded.memoryNote
                rebuildInterface()
                if let aiError = envelope.aiError { statusLabel.stringValue = aiError }
                return
            }
            if let aiError = envelope.aiError { storySuggestionError = aiError }
            loadData(finalStatus: envelope.aiError ?? "已保存到本地数据库")
            return
        }
        if let decoded = decodePayload(data) {
            payload = decoded
            folderNote.string = decoded.memoryNote
            rebuildInterface()
        } else {
            showBridgeError("本地 Story 数据格式无法读取")
        }
    }
    private func showBridgeError(_ message: String) { statusLabel.stringValue = message.isEmpty ? "操作失败，可重试" : "操作失败：\(message)" }

    private func rebuildInterface() { updateSidebarSummary(); footageTable.reloadData(); rebuildCanvas(); renderInspector(); updateFlowStepper() }
    private func updateSidebarSummary() {
        guard let payload else { return }
        let used = Set(payload.story?.beats.flatMap { $0.clips.map(\.clipID) } ?? []).count
        let beats = payload.story?.beats.count ?? 0
        projectTitleLabel.stringValue = folder.lastPathComponent
        let reviewed = payload.items.filter { isReviewed($0) }.count
        projectMetaLabel.stringValue = "\(reviewed) 已查看 · \(payload.items.filter { hasCreatorNote($0) }.count) 条随手记"
        let noteCount = payload.items.filter { hasCreatorNote($0) }.count
        let entries: [(String, String, Int)] = [
            ("all", "全部素材", payload.items.count),
            ("reviewed", "已查看", reviewed),
            ("notes", "有随手记", noteCount),
        ]
        for (id, label, count) in entries {
            guard let button = navigationButtons[id] else { continue }
            let active = id == filter
            navigationNameLabels[id]?.stringValue = label
            navigationNameLabels[id]?.font = NSFont.systemFont(ofSize: 12.5, weight: active ? .semibold : .regular)
            navigationNameLabels[id]?.textColor = active ? Theme.brandForest : .secondaryLabelColor
            navigationCountLabels[id]?.stringValue = String(count)
            navigationCountLabels[id]?.textColor = active ? Theme.brandMoss : .tertiaryLabelColor
            button.layer?.backgroundColor = active ? Theme.brandLime.withAlphaComponent(0.18).cgColor : NSColor.clear.cgColor
        }
        footagePoolCountLabel.stringValue = workflowStep == 1 ? "\(visibleClips().count) 段素材" : "\(used) 已用 · \(beats) 段故事"
    }
    private func rebuildCanvas() {
        canvasStack.arrangedSubviews.forEach { canvasStack.removeArrangedSubview($0); $0.removeFromSuperview() }
        fieldBindings.removeAll(); guard let payload else { return }
        switch workflowStep {
        case 1: renderReviewFootage(payload)
        case 2: renderGenerateStory(payload)
        case 4: renderPreviewLanding(payload)
        default:
            if storyGenerationBusy { renderStoryGenerationLoading() }
            else if let story = payload.story { renderStory(story, payload: payload) }
            else { renderEmptyStory() }
        }
    }
    private func addCanvasView(_ view: NSView, fillWidth: Bool = true) { canvasStack.addArrangedSubview(view); if fillWidth { view.widthAnchor.constraint(equalTo: canvasStack.widthAnchor).isActive = true } }

    /// 对话面板随检查栏伸展，消息区吸收高度，输入区始终贴近底部。
    private func addInspectorPanel(_ panel: NSView, fillsHeight: Bool = false) {
        inspectorStack.addArrangedSubview(panel)
        panel.widthAnchor.constraint(equalTo: inspectorStack.widthAnchor).isActive = true
        if fillsHeight { panel.heightAnchor.constraint(greaterThanOrEqualTo: inspectorScroll.contentView.heightAnchor, constant: -18).isActive = true }
    }

    /// 所有品牌徽标都使用独立容器，避免 AppKit 文字基线导致的视觉偏心。
    private func makeCenteredBadge(_ text: String, font: NSFont, textColor: NSColor, fill: NSColor, size: NSSize, cornerRadius: CGFloat, borderColor: NSColor? = nil, borderWidth: CGFloat = 0) -> NSView {
        let badge = NSView(); badge.wantsLayer = true; badge.layer?.backgroundColor = fill.cgColor; badge.layer?.cornerRadius = cornerRadius; badge.layer?.masksToBounds = true; badge.layer?.borderColor = borderColor?.cgColor; badge.layer?.borderWidth = borderWidth; badge.translatesAutoresizingMaskIntoConstraints = false
        let glyph = NSTextField(labelWithString: text); glyph.font = font; glyph.textColor = textColor; glyph.alignment = .center; glyph.usesSingleLineMode = true; glyph.lineBreakMode = .byClipping; glyph.translatesAutoresizingMaskIntoConstraints = false
        badge.addSubview(glyph)
        NSLayoutConstraint.activate([badge.widthAnchor.constraint(equalToConstant: size.width), badge.heightAnchor.constraint(equalToConstant: size.height), glyph.centerXAnchor.constraint(equalTo: badge.centerXAnchor), glyph.centerYAnchor.constraint(equalTo: badge.centerYAnchor)])
        return badge
    }

    private func reviewStatus(_ clip: DeskClip) -> String { reviewStatusDrafts[clip.id] ?? clip.noteStatus }
    private func isReviewed(_ clip: DeskClip) -> Bool { reviewStatus(clip) == "done" || reviewStatus(clip) == "skipped" }

    private func renderReviewFootage(_ payload: DeskPayload) {
        let clip = selectedReviewClip(in: payload)
        let title = NSTextField(wrappingLabelWithString: clip?.title ?? "先从左侧选择一段素材"); title.font = Theme.largeTitle(); title.maximumNumberOfLines = 2
        let summary = NSTextField(wrappingLabelWithString: clip?.detail ?? "选中素材后，在这里查看完整画面与识别摘要。"); summary.font = Theme.body(); summary.textColor = .secondaryLabelColor; summary.maximumNumberOfLines = 3
        addCanvasView(title)
        addCanvasView(summary)
        reviewPlayer.controlsStyle = .floating; reviewPlayer.wantsLayer = true; reviewPlayer.layer?.backgroundColor = NSColor.black.cgColor; reviewPlayer.layer?.cornerRadius = 9; reviewPlayer.layer?.masksToBounds = true; reviewPlayer.translatesAutoresizingMaskIntoConstraints = false
        if reviewPlayer.constraints.first(where: { $0.firstAttribute == .height && $0.secondAttribute == .width }) == nil { reviewPlayer.heightAnchor.constraint(equalTo: reviewPlayer.widthAnchor, multiplier: 9.0 / 16.0).isActive = true }
        addCanvasView(reviewPlayer)
        if let clip { setReviewPlayer(clip) }
        if let clip { addCanvasView(makeTagPills(clip.visualTags + clip.narrativeTags)) }
    }

    private func makeTagPills(_ tags: [String]) -> NSView {
        let wrap = NSView(); wrap.translatesAutoresizingMaskIntoConstraints = false
        let strip = NSStackView(); strip.orientation = .horizontal; strip.alignment = .centerY; strip.spacing = Theme.space2
        for tag in tags.filter({ !$0.isEmpty }).prefix(6) {
            let pill = NSTextField(labelWithString: "  \(tag)  "); pill.font = NSFont.systemFont(ofSize: 9.5, weight: .medium); pill.textColor = Theme.brandMoss; pill.alignment = .center; pill.wantsLayer = true; pill.layer?.backgroundColor = Theme.brandLime.withAlphaComponent(0.20).cgColor; pill.layer?.borderColor = Theme.brandLime.withAlphaComponent(Theme.borderAlpha).cgColor; pill.layer?.borderWidth = 0.5; pill.layer?.cornerRadius = Theme.radiusChip; pill.layer?.masksToBounds = true; pill.heightAnchor.constraint(equalToConstant: 22).isActive = true; strip.addArrangedSubview(pill)
        }
        strip.translatesAutoresizingMaskIntoConstraints = false; wrap.addSubview(strip)
        NSLayoutConstraint.activate([wrap.heightAnchor.constraint(equalToConstant: 26), strip.centerXAnchor.constraint(equalTo: wrap.centerXAnchor), strip.centerYAnchor.constraint(equalTo: wrap.centerYAnchor), strip.leadingAnchor.constraint(greaterThanOrEqualTo: wrap.leadingAnchor), strip.trailingAnchor.constraint(lessThanOrEqualTo: wrap.trailingAnchor)])
        return wrap
    }

    private func makeReviewInfo(_ heading: String, _ value: String) -> NSView {
        let card = NSView(); card.wantsLayer = true
        let label = smallLabel(heading); let body = inspectorBody(value); body.font = .systemFont(ofSize: 11.5); body.maximumNumberOfLines = 4
        label.translatesAutoresizingMaskIntoConstraints = false; body.translatesAutoresizingMaskIntoConstraints = false; card.addSubview(label); card.addSubview(body)
        NSLayoutConstraint.activate([card.heightAnchor.constraint(equalToConstant: 92), label.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 10), label.topAnchor.constraint(equalTo: card.topAnchor, constant: 9), body.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 10), body.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -10), body.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 5), body.bottomAnchor.constraint(lessThanOrEqualTo: card.bottomAnchor, constant: -8)])
        return card
    }

    private func selectedReviewClip(in payload: DeskPayload) -> DeskClip? {
        if let id = selectedClipID, let clip = payload.items.first(where: { $0.id == id }) { return clip }
        return payload.items.first
    }

    private func setReviewPlayer(_ clip: DeskClip) {
        let path = mediaPath(for: clip)
        if !path.isEmpty { reviewPlayer.player = AVPlayer(url: URL(fileURLWithPath: path)) }
    }

    private func renderGenerateStory(_ payload: DeskPayload) {
        let title = NSTextField(labelWithString: "让 AI 提方向，也可以告诉 Reelsift 你想讲什么"); title.font = .systemFont(ofSize: 24, weight: .bold)
        let body = NSTextField(wrappingLabelWithString: "在右侧和 AI 聊聊想讲什么、什么风格、有什么要求；生成的方向会显示在这里。")
        body.font = .systemFont(ofSize: 13); body.textColor = .secondaryLabelColor; body.maximumNumberOfLines = 2
        let stats = NSTextField(labelWithString: "\(payload.items.count) 素材 · \(payload.items.filter { isReviewed($0) }.count) 已查看 · \(payload.items.filter { hasCreatorNote($0) }.count) 条 Creator Notes")
        stats.font = .systemFont(ofSize: 13, weight: .medium); stats.textColor = .secondaryLabelColor
        let notes = payload.items.filter { hasCreatorNote($0) }.map { "✎ \($0.filename): \(displayedClipNote($0))" }.joined(separator: "\n")
        let context = NSTextField(wrappingLabelWithString: notes.isEmpty ? "没有随手记；AI 将仅根据已识别的素材事实生成。" : notes); context.font = .systemFont(ofSize: 12); context.textColor = .secondaryLabelColor; context.maximumNumberOfLines = 6
        addCanvasView(title); addCanvasView(body); addCanvasView(stats); addCanvasView(divider()); addCanvasView(context)
        if storyDirections.isEmpty {
            let empty = NSView(); Theme.applyCardStyle(empty, cornerRadius: Theme.radiusPanel, background: NSColor.controlBackgroundColor)
            let mark = makeCenteredBadge("✦", font: Theme.largeTitle(), textColor: Theme.brandForest, fill: Theme.brandLime, size: NSSize(width: 44, height: 44), cornerRadius: 22)
            let emptyTitle = NSTextField(labelWithString: "先和右侧导演 Agent 说说你的想法"); emptyTitle.font = Theme.subhead(); emptyTitle.alignment = .center; emptyTitle.translatesAutoresizingMaskIntoConstraints = false
            let emptyBody = NSTextField(wrappingLabelWithString: "可以说主题、风格、时长或一句情绪；发送后，这里会出现三种可选的叙事方向。  →"); emptyBody.font = Theme.body(); emptyBody.textColor = .secondaryLabelColor; emptyBody.alignment = .center; emptyBody.maximumNumberOfLines = 2; emptyBody.translatesAutoresizingMaskIntoConstraints = false
            empty.addSubview(mark); empty.addSubview(emptyTitle); empty.addSubview(emptyBody)
            NSLayoutConstraint.activate([empty.heightAnchor.constraint(equalToConstant: 218), mark.centerXAnchor.constraint(equalTo: empty.centerXAnchor), mark.topAnchor.constraint(equalTo: empty.topAnchor, constant: 36), emptyTitle.topAnchor.constraint(equalTo: mark.bottomAnchor, constant: Theme.space3), emptyTitle.leadingAnchor.constraint(equalTo: empty.leadingAnchor, constant: Theme.space5), emptyTitle.trailingAnchor.constraint(equalTo: empty.trailingAnchor, constant: -Theme.space5), emptyBody.topAnchor.constraint(equalTo: emptyTitle.bottomAnchor, constant: Theme.space2), emptyBody.leadingAnchor.constraint(equalTo: empty.leadingAnchor, constant: 64), emptyBody.trailingAnchor.constraint(equalTo: empty.trailingAnchor, constant: -64)])
            addCanvasView(empty)
        } else {
            let cards = NSStackView(); cards.orientation = .horizontal; cards.alignment = .top; cards.distribution = .fillEqually; cards.spacing = 10
            cards.setContentHuggingPriority(.defaultLow, for: .horizontal)
            cards.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            for (index, direction) in storyDirections.enumerated() {
                let card = makeDirectionCard(direction, index: index)
                card.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
                cards.addArrangedSubview(card)
            }
            addCanvasView(cards)
        }
    }

    private func renderStoryGenerationLoading() {
        let title = NSTextField(labelWithString: "正在生成 Story Beats…"); title.font = Theme.largeTitle()
        let body = NSTextField(wrappingLabelWithString: "已选方向：\(pendingDirectionTitle.isEmpty ? "当前方向" : pendingDirectionTitle)。你可以切换到其他步骤查看素材，生成会在后台继续。")
        body.font = Theme.body(); body.textColor = .secondaryLabelColor; body.maximumNumberOfLines = 2
        let spinner = NSProgressIndicator(); spinner.style = .spinning; spinner.controlSize = .regular; spinner.startAnimation(nil)
        let status = NSTextField(labelWithString: "正在根据素材、随手记和所选方向编排故事段落…"); status.font = Theme.body(); status.textColor = .secondaryLabelColor
        let card = NSStackView(views: [spinner, status]); card.orientation = .horizontal; card.alignment = .centerY; card.spacing = Theme.space3
        addCanvasView(title); addCanvasView(body); addCanvasView(card)
    }

    private func makeBriefField() -> NSView {
        let wrap = NSView(); Theme.applyCardStyle(wrap, cornerRadius: Theme.radiusCard)
        let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.translatesAutoresizingMaskIntoConstraints = false
        // NSTextView 复用到不同步骤后，必须每次明确恢复可输入状态。
        storyBriefField.font = .systemFont(ofSize: 12.5); storyBriefField.textColor = .labelColor; storyBriefField.isRichText = false; storyBriefField.isEditable = !directionsBusy; storyBriefField.isSelectable = true; storyBriefField.drawsBackground = false; storyBriefField.textContainerInset = NSSize(width: 1, height: 6); storyBriefField.textContainer?.widthTracksTextView = true
        storyBriefField.placeholderString = "想讲什么、什么风格、有什么要求，都可以写在这里，例如：普通工作日也有自己的生活感，风格克制自然不要鸡汤，控制在 90 秒左右"
        // NSTextView 作为 NSScrollView 的 document view 时不会自动取得可点击尺寸。
        storyBriefField.frame = scroll.contentView.bounds
        storyBriefField.autoresizingMask = [.width, .height]
        storyBriefField.minSize = .zero
        storyBriefField.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        scroll.documentView = storyBriefField
        wrap.setContentHuggingPriority(.required, for: .vertical)
        wrap.setContentCompressionResistancePriority(.required, for: .vertical)
        wrap.addSubview(scroll)
        NSLayoutConstraint.activate([wrap.heightAnchor.constraint(equalToConstant: 84), scroll.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 10), scroll.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -10), scroll.topAnchor.constraint(equalTo: wrap.topAnchor, constant: 4), scroll.bottomAnchor.constraint(equalTo: wrap.bottomAnchor, constant: -4)])
        return wrap
    }

    private func currentDirectionBrief() -> String {
        directionChatMessages.filter { $0.role == "user" }.map(\.text).joined(separator: "\n")
    }

    private func makeSimpleChatBubble(role: String, text: String) -> NSView {
        let isUser = role == "user"
        let bubble = NSView(); bubble.wantsLayer = true
        bubble.layer?.backgroundColor = (isUser ? Theme.brandLime.withAlphaComponent(0.90) : NSColor.controlBackgroundColor).cgColor
        bubble.layer?.cornerRadius = Theme.radiusCard
        bubble.layer?.borderWidth = 0.5; bubble.layer?.borderColor = (isUser ? Theme.brandLime : NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha)).cgColor
        let label = NSTextField(wrappingLabelWithString: text); label.font = Theme.body(); label.textColor = isUser ? Theme.brandForest : .labelColor; label.maximumNumberOfLines = 0; label.translatesAutoresizingMaskIntoConstraints = false
        bubble.addSubview(label)
        bubble.widthAnchor.constraint(lessThanOrEqualToConstant: 230).isActive = true
        NSLayoutConstraint.activate([label.leadingAnchor.constraint(equalTo: bubble.leadingAnchor, constant: 10), label.trailingAnchor.constraint(equalTo: bubble.trailingAnchor, constant: -10), label.topAnchor.constraint(equalTo: bubble.topAnchor, constant: 7), label.bottomAnchor.constraint(equalTo: bubble.bottomAnchor, constant: -7)])
        let row = NSStackView(); row.orientation = .horizontal; row.alignment = .top; row.spacing = Theme.space2; row.translatesAutoresizingMaskIntoConstraints = false
        let spacer = NSView()
        let avatar = makeChatAvatar(isUser: isUser)
        if isUser { row.addArrangedSubview(spacer); row.addArrangedSubview(bubble); row.addArrangedSubview(avatar) } else { row.addArrangedSubview(avatar); row.addArrangedSubview(bubble); row.addArrangedSubview(spacer) }
        spacer.widthAnchor.constraint(greaterThanOrEqualToConstant: 12).isActive = true
        return row
    }

    private func makeDirectionChatPanel(_ payload: DeskPayload) -> NSView {
        let wrap = NSView(); Theme.applyCardStyle(wrap, cornerRadius: Theme.radiusPanel)
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 7; content.translatesAutoresizingMaskIntoConstraints = false
        let heading = NSTextField(labelWithString: "✦  和 AI 聊聊方向"); heading.font = Theme.body(); heading.textColor = .systemPurple
        content.addArrangedSubview(heading)
        let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.hasVerticalScroller = true; scroll.translatesAutoresizingMaskIntoConstraints = false
        let messagesStack = NSStackView(); messagesStack.orientation = .vertical; messagesStack.alignment = .width; messagesStack.spacing = 8; messagesStack.translatesAutoresizingMaskIntoConstraints = false
        let flipped = FlippedView(); flipped.translatesAutoresizingMaskIntoConstraints = false; flipped.addSubview(messagesStack)
        if directionChatMessages.isEmpty {
            messagesStack.addArrangedSubview(inspectorBody("想讲什么、什么风格、有什么要求，都可以直接说，例如：普通工作日也有自己的生活感，风格克制自然不要鸡汤，控制在 90 秒左右。留空直接发送，AI 会自由提三个方向。"))
        } else {
            for message in directionChatMessages { messagesStack.addArrangedSubview(makeSimpleChatBubble(role: message.role, text: message.text)) }
        }
        scroll.documentView = flipped
        // 消息区是唯一可以吸收多余高度的区域，输入区因此稳定贴住卡片底部。
        scroll.setContentHuggingPriority(NSLayoutConstraint.Priority(rawValue: 1), for: .vertical); scroll.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        NSLayoutConstraint.activate([scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 180), flipped.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor), flipped.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor), messagesStack.leadingAnchor.constraint(equalTo: flipped.leadingAnchor, constant: 2), messagesStack.trailingAnchor.constraint(equalTo: flipped.trailingAnchor, constant: -2), messagesStack.topAnchor.constraint(equalTo: flipped.topAnchor, constant: 2), messagesStack.bottomAnchor.constraint(equalTo: flipped.bottomAnchor, constant: -2)])
        content.addArrangedSubview(scroll)
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .vertical); spacer.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        content.addArrangedSubview(spacer)
        content.addArrangedSubview(makeBriefField())
        generateDirectionsButton.title = storyDirections.isEmpty ? "✦  发送并生成 3 个方向" : "↻  发送并重新生成"
        generateDirectionsButton.target = self; generateDirectionsButton.action = #selector(generateDirections)
        generateDirectionsButton.isEnabled = !directionsBusy
        Theme.applyPrimaryButton(generateDirectionsButton); generateDirectionsButton.controlSize = .large
        generateDirectionsSpinner.style = .spinning; generateDirectionsSpinner.controlSize = .small; generateDirectionsSpinner.isDisplayedWhenStopped = false
        let buttonRow = NSStackView(views: [generateDirectionsButton, generateDirectionsSpinner]); buttonRow.orientation = .horizontal; buttonRow.alignment = .centerY; buttonRow.spacing = 8; buttonRow.translatesAutoresizingMaskIntoConstraints = false
        content.addArrangedSubview(buttonRow)
        wrap.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: wrap.topAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: wrap.bottomAnchor, constant: -12)])
        return wrap
    }

    private func makeDirectionCard(_ direction: StoryDirection, index: Int) -> NSView {
        let accent = Theme.accent(index)
        let card = NSView(); Theme.applyCardStyle(card, cornerRadius: Theme.radiusPanel, background: NSColor.controlBackgroundColor)
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = Theme.space2; content.translatesAutoresizingMaskIntoConstraints = false
        let number = makeCenteredBadge(String(format: "%02d", index + 1), font: .monospacedDigitSystemFont(ofSize: 10, weight: .bold), textColor: index == 0 ? Theme.brandForest : .white, fill: accent, size: NSSize(width: 28, height: 20), cornerRadius: Theme.radiusChip)
        let title = inspectorTitle(direction.title); title.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let style = inspectorBody(direction.style.isEmpty ? "—" : direction.style); style.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let styleBlock = NSStackView(); styleBlock.orientation = .vertical; styleBlock.alignment = .width; styleBlock.spacing = 4; styleBlock.addArrangedSubview(inspectorLabel("风格")); styleBlock.addArrangedSubview(style)
        content.addArrangedSubview(number)
        content.addArrangedSubview(title)
        content.addArrangedSubview(styleBlock)
        content.addArrangedSubview(makeStructureBar(direction.structure, accent: accent))
        let choose = NSButton(title: "选择这个方向", target: self, action: #selector(chooseDirection(_:))); choose.tag = index; Theme.applyPrimaryButton(choose); choose.heightAnchor.constraint(equalToConstant: 32).isActive = true; content.addArrangedSubview(choose)
        card.addSubview(content)
        NSLayoutConstraint.activate([card.heightAnchor.constraint(greaterThanOrEqualToConstant: 170), content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: card.topAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -12)])
        return card
    }

    private func makeStructureBar(_ stages: [String], accent: NSColor) -> NSView {
        let group = NSStackView(); group.orientation = .vertical; group.alignment = .width; group.spacing = Theme.space1
        group.addArrangedSubview(smallLabel("结构"))
        let segments = NSStackView(); segments.orientation = .horizontal; segments.alignment = .centerY; segments.distribution = .fillEqually; segments.spacing = 3
        segments.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        for stage in stages.prefix(5) {
            let segment = NSTextField(labelWithString: stage); segment.font = Theme.caption(); segment.textColor = .labelColor; segment.alignment = .center; segment.usesSingleLineMode = true; segment.lineBreakMode = .byTruncatingTail; segment.setContentCompressionResistancePriority(.defaultLow, for: .horizontal); segment.wantsLayer = true; segment.layer?.backgroundColor = accent.withAlphaComponent(0.22).cgColor; segment.layer?.cornerRadius = Theme.radiusChip; segment.layer?.masksToBounds = true; segment.heightAnchor.constraint(equalToConstant: 22).isActive = true; segments.addArrangedSubview(segment)
        }
        if stages.isEmpty { segments.addArrangedSubview(inspectorBody("未提供结构")) }
        group.addArrangedSubview(segments)
        return group
    }

    private func makeChatAvatar(isUser: Bool) -> NSView {
        let avatar = NSView(); avatar.wantsLayer = true; avatar.layer?.cornerRadius = 11; avatar.layer?.masksToBounds = true; avatar.layer?.borderWidth = isUser ? 0 : 1.25; avatar.layer?.borderColor = Theme.brandLime.cgColor; avatar.layer?.backgroundColor = isUser ? Theme.brandForest.cgColor : NSColor.clear.cgColor; avatar.translatesAutoresizingMaskIntoConstraints = false
        let glyph = NSTextField(labelWithString: isUser ? "●" : "✦"); glyph.font = .systemFont(ofSize: isUser ? 8 : 11, weight: .bold); glyph.alignment = .center; glyph.textColor = isUser ? Theme.brandLime : Theme.brandForest; glyph.translatesAutoresizingMaskIntoConstraints = false
        avatar.addSubview(glyph)
        NSLayoutConstraint.activate([avatar.widthAnchor.constraint(equalToConstant: 22), avatar.heightAnchor.constraint(equalToConstant: 22), glyph.centerXAnchor.constraint(equalTo: avatar.centerXAnchor), glyph.centerYAnchor.constraint(equalTo: avatar.centerYAnchor)])
        return avatar
    }

    private func renderPreviewLanding(_ payload: DeskPayload) {
        let title = NSTextField(labelWithString: "预览故事"); title.font = Theme.largeTitle()
        let body = NSTextField(wrappingLabelWithString: "按照当前 Story Beat 和素材顺序预览。它用于验证故事，而不是替代剪辑器。")
        body.font = .systemFont(ofSize: 13); body.textColor = .secondaryLabelColor
        addCanvasView(title); addCanvasView(body)
        reviewPlayer.controlsStyle = .floating; reviewPlayer.wantsLayer = true; reviewPlayer.layer?.backgroundColor = NSColor.black.cgColor; reviewPlayer.layer?.cornerRadius = 9; reviewPlayer.layer?.masksToBounds = true; reviewPlayer.translatesAutoresizingMaskIntoConstraints = false
        if reviewPlayer.constraints.first(where: { $0.firstAttribute == .height && $0.secondAttribute == .width }) == nil { reviewPlayer.heightAnchor.constraint(equalTo: reviewPlayer.widthAnchor, multiplier: 9.0 / 16.0).isActive = true }
        addCanvasView(reviewPlayer)
        let button = NSButton(title: "▶  开始预览", target: self, action: #selector(previewStory)); Theme.applyPrimaryButton(button); button.controlSize = .large
        addCanvasView(button, fillWidth: false)
        if payload.story == nil { body.stringValue = "先进入 Generate Story 生成 Story Beats 后再预览。" }
    }
    private func renderEmptyStory() { let title = NSTextField(labelWithString: "故事画布"); title.font = Theme.largeTitle(); let body = NSTextField(wrappingLabelWithString: "把真实素材拖到故事中；Reelsift 会从视觉摘要、口播、随手记和叙事标签生成第一版结构。"); body.font = Theme.subhead(); body.textColor = .secondaryLabelColor; body.maximumNumberOfLines = 0; let button = NSButton(title: "生成故事", target: self, action: #selector(goToGenerateStoryStep)); Theme.applyPrimaryButton(button); button.controlSize = .large; addCanvasView(title); addCanvasView(body); addCanvasView(button, fillWidth: false) }
    private func renderStory(_ story: Story, payload: DeskPayload) {
        let header = NSView()
        let eyebrow = smallLabel("STORY")
        let title = NSTextField(labelWithString: story.title); title.font = .systemFont(ofSize: 23, weight: .bold); title.translatesAutoresizingMaskIntoConstraints = false
        let thesis = NSTextField(wrappingLabelWithString: story.thesis.isEmpty ? "尚未设置故事方向" : story.thesis); thesis.font = .systemFont(ofSize: 12.5); thesis.textColor = .secondaryLabelColor; thesis.maximumNumberOfLines = 2; thesis.translatesAutoresizingMaskIntoConstraints = false
        let more = NSButton(title: "•••", target: self, action: #selector(showStoryMenu(_:))); more.isBordered = false; more.font = .systemFont(ofSize: 14, weight: .semibold); more.toolTip = "Story actions"; more.translatesAutoresizingMaskIntoConstraints = false
        header.addSubview(eyebrow); header.addSubview(title); header.addSubview(thesis); header.addSubview(more)
        NSLayoutConstraint.activate([header.heightAnchor.constraint(greaterThanOrEqualToConstant: 70), eyebrow.leadingAnchor.constraint(equalTo: header.leadingAnchor), eyebrow.topAnchor.constraint(equalTo: header.topAnchor), title.leadingAnchor.constraint(equalTo: header.leadingAnchor), title.topAnchor.constraint(equalTo: eyebrow.bottomAnchor, constant: 2), more.trailingAnchor.constraint(equalTo: header.trailingAnchor), more.topAnchor.constraint(equalTo: header.topAnchor, constant: 5), thesis.leadingAnchor.constraint(equalTo: header.leadingAnchor), thesis.trailingAnchor.constraint(equalTo: more.leadingAnchor, constant: -12), thesis.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 3), thesis.bottomAnchor.constraint(equalTo: header.bottomAnchor)])
        addCanvasView(header)
        addCanvasView(makeStoryThumbnailStrip(story, payload: payload))
        story.beats.sorted { $0.order < $1.order }.enumerated().forEach { i, beat in addCanvasView(makeBeatView(beat, index: i + 1, story: story, payload: payload)) }
        let unused = DropZoneView(label: "拖到这里，归还到未使用素材"); unused.onClip = { [weak self] id in self?.moveClip(id, story: story.id, targetBeat: 0, position: nil) }; unused.heightAnchor.constraint(equalToConstant: 34).isActive = true; addCanvasView(unused)
    }

    private func makeStoryThumbnailStrip(_ story: Story, payload: DeskPayload) -> NSView {
        let scroll = NSScrollView(); scroll.drawsBackground = false; scroll.hasHorizontalScroller = false; scroll.autohidesScrollers = true
        let strip = NSStackView(); strip.orientation = .horizontal; strip.alignment = .centerY; strip.spacing = Theme.space2; strip.translatesAutoresizingMaskIntoConstraints = false
        for (index, beat) in story.beats.sorted(by: { $0.order < $1.order }).enumerated() {
            let thumb = NSButton(title: "", target: self, action: #selector(selectStoryBeatThumbnail(_:)))
            thumb.tag = beat.id; thumb.toolTip = "跳转到 Beat \(index + 1)：\(beat.title)"; thumb.isBordered = false; thumb.wantsLayer = true; thumb.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor; thumb.layer?.cornerRadius = Theme.radiusChip; thumb.layer?.borderWidth = selectedBeatID == beat.id ? 1.5 : 0.5; thumb.layer?.borderColor = (selectedBeatID == beat.id ? Theme.brandLime : NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha)).cgColor; thumb.imageScaling = .scaleProportionallyUpOrDown; thumb.imagePosition = .imageOnly
            if let clipID = beat.clips.sorted(by: { $0.order < $1.order }).first?.clipID, let clip = payload.items.first(where: { $0.id == clipID }) { thumb.image = NSImage(contentsOfFile: clip.coverPath) }
            thumb.widthAnchor.constraint(equalToConstant: 58).isActive = true; thumb.heightAnchor.constraint(equalToConstant: 38).isActive = true
            strip.addArrangedSubview(thumb)
        }
        let document = FlippedView(); document.addSubview(strip); scroll.documentView = document
        NSLayoutConstraint.activate([scroll.heightAnchor.constraint(equalToConstant: 42), document.heightAnchor.constraint(equalTo: scroll.contentView.heightAnchor), document.widthAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.widthAnchor), strip.leadingAnchor.constraint(equalTo: document.leadingAnchor), strip.trailingAnchor.constraint(equalTo: document.trailingAnchor), strip.centerYAnchor.constraint(equalTo: document.centerYAnchor)])
        return scroll
    }

    @objc private func selectStoryBeatThumbnail(_ sender: NSButton) { showBeat(sender.tag) }
    private func makeBeatView(_ beat: StoryBeat, index: Int, story: Story, payload: DeskPayload) -> NSView {
        let wrapper = DropZoneView(label: "")
        let isSelected = selectedBeatID == beat.id
        wrapper.setBaseStyle(borderWidth: isSelected ? 1 : 0.5, cornerRadius: Theme.radiusCard, backgroundColor: isSelected ? Theme.brandLime.withAlphaComponent(0.10) : .textBackgroundColor)
        if isSelected { wrapper.layer?.borderColor = Theme.brandLime.withAlphaComponent(Theme.borderAlpha).cgColor }
        wrapper.onClip = { [weak self] id in self?.moveClip(id, story: story.id, targetBeat: beat.id, position: beat.clips.count + 1) }
        wrapper.onBeat = { [weak self] id in self?.moveBeat(id, story: story, toIndex: index - 1) }
        let rail = NSView(); rail.wantsLayer = true; rail.layer?.backgroundColor = Theme.brandLime.withAlphaComponent(0.62).cgColor; rail.layer?.cornerRadius = 1; rail.translatesAutoresizingMaskIntoConstraints = false
        let handle = BeatHandleView(index: index, beatID: beat.id, select: { [weak self] in self?.showBeat(beat.id) }); handle.stringValue = "⠿"; handle.font = .systemFont(ofSize: 11); handle.translatesAutoresizingMaskIntoConstraints = false
        let badge = makeCenteredBadge(String(format: "%02d", index), font: .monospacedDigitSystemFont(ofSize: 10, weight: .bold), textColor: Theme.brandForest, fill: Theme.brandLime, size: NSSize(width: 24, height: 24), cornerRadius: 12)
        let title = textField(beat.title, font: Theme.subhead(), placeholder: "Beat 标题", lines: 1); bind(title, beatID: beat.id, key: "title")
        let intentLabel = smallLabel("INTENT")
        let intent = textField(beat.intent, font: Theme.small(), placeholder: "这个 Beat 想表达什么？", lines: 1); bind(intent, beatID: beat.id, key: "intent")
        let intentGroup = NSStackView(views: [intentLabel, intent]); intentGroup.orientation = .vertical; intentGroup.alignment = .leading; intentGroup.spacing = 1
        let clipCount = NSTextField(labelWithString: "\(orderedClipCount(beat)) 段素材"); clipCount.font = Theme.small(); clipCount.textColor = .secondaryLabelColor; clipCount.alignment = .right; clipCount.translatesAutoresizingMaskIntoConstraints = false
        let saved = NSTextField(labelWithString: "✓ 已保存"); saved.font = Theme.caption(); saved.textColor = .systemGreen; saved.translatesAutoresizingMaskIntoConstraints = false
        let header = NSStackView(views: [handle, badge, title, intentGroup, NSView(), clipCount, saved]); header.orientation = .horizontal; header.alignment = .top; header.spacing = Theme.space2; header.distribution = .fill; header.translatesAutoresizingMaskIntoConstraints = false
        title.widthAnchor.constraint(equalToConstant: 118).isActive = true; intentGroup.widthAnchor.constraint(equalToConstant: 220).isActive = true; clipCount.widthAnchor.constraint(equalToConstant: 52).isActive = true

        // 文案是每个 Beat 的主编辑区：占满整行并固定显示多行，不再压在标题右侧。
        let scriptLabel = smallLabel("SCRIPT / 文案")
        let script = textField(beat.script, font: Theme.body(), placeholder: "写下这段旁白、画面说明或节奏提示…", lines: 0); bind(script, beatID: beat.id, key: "script")
        script.wantsLayer = true; script.layer?.backgroundColor = NSColor.controlBackgroundColor.withAlphaComponent(0.72).cgColor; script.layer?.borderColor = NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha).cgColor; script.layer?.borderWidth = 0.5; script.layer?.cornerRadius = Theme.radiusChip
        let scriptGroup = NSStackView(views: [scriptLabel, script]); scriptGroup.orientation = .vertical; scriptGroup.alignment = .width; scriptGroup.spacing = 4; scriptGroup.translatesAutoresizingMaskIntoConstraints = false
        let stripScroll = NSScrollView(); stripScroll.drawsBackground = false; stripScroll.hasHorizontalScroller = true; stripScroll.autohidesScrollers = true; stripScroll.translatesAutoresizingMaskIntoConstraints = false
        let stripDocument = FlippedView(); stripDocument.translatesAutoresizingMaskIntoConstraints = false; stripScroll.documentView = stripDocument
        let clips = NSStackView(); clips.orientation = .horizontal; clips.alignment = .top; clips.spacing = 2; clips.translatesAutoresizingMaskIntoConstraints = false; stripDocument.addSubview(clips)
        let orderedLinks = beat.clips.sorted { $0.order < $1.order }
        for (clipIndex, link) in orderedLinks.enumerated() {
            let insertion = makeClipInsertionZone(storyID: story.id, beatID: beat.id, position: clipIndex)
            clips.addArrangedSubview(insertion)
            if let clip = payload.items.first(where: { $0.id == link.clipID }) {
                clips.addArrangedSubview(BeatClipCardView(clip: clip, selected: selectedClipID == clip.id, select: { [weak self] in self?.showClip(clip) }))
            }
        }
        clips.addArrangedSubview(makeClipInsertionZone(storyID: story.id, beatID: beat.id, position: orderedLinks.count))
        let add = DropZoneView(label: orderedLinks.isEmpty ? "拖拽素材到这里" : "+ 添加素材"); add.setBaseStyle(borderWidth: 0.5, cornerRadius: Theme.radiusChip, backgroundColor: .controlBackgroundColor); add.onClip = { [weak self] id in self?.moveClip(id, story: story.id, targetBeat: beat.id, position: orderedLinks.count + 1) }; NSLayoutConstraint.activate([add.widthAnchor.constraint(equalToConstant: orderedLinks.isEmpty ? 152 : 104), add.heightAnchor.constraint(equalToConstant: 94)]); clips.addArrangedSubview(add)
        wrapper.addSubview(rail); wrapper.addSubview(header); wrapper.addSubview(scriptGroup); wrapper.addSubview(stripScroll)
        NSLayoutConstraint.activate([wrapper.heightAnchor.constraint(equalToConstant: 254), rail.leadingAnchor.constraint(equalTo: wrapper.leadingAnchor, constant: 31), rail.topAnchor.constraint(equalTo: wrapper.topAnchor), rail.bottomAnchor.constraint(equalTo: wrapper.bottomAnchor), rail.widthAnchor.constraint(equalToConstant: 2), header.leadingAnchor.constraint(equalTo: wrapper.leadingAnchor, constant: 12), header.trailingAnchor.constraint(equalTo: wrapper.trailingAnchor, constant: -12), header.topAnchor.constraint(equalTo: wrapper.topAnchor, constant: 11), header.heightAnchor.constraint(equalToConstant: 31), handle.widthAnchor.constraint(equalToConstant: 12), intent.widthAnchor.constraint(equalTo: intentGroup.widthAnchor), intent.heightAnchor.constraint(equalToConstant: 17), scriptGroup.leadingAnchor.constraint(equalTo: wrapper.leadingAnchor, constant: 60), scriptGroup.trailingAnchor.constraint(equalTo: wrapper.trailingAnchor, constant: -12), scriptGroup.topAnchor.constraint(equalTo: header.bottomAnchor, constant: 5), script.heightAnchor.constraint(equalToConstant: 64), stripScroll.leadingAnchor.constraint(equalTo: wrapper.leadingAnchor, constant: 60), stripScroll.trailingAnchor.constraint(equalTo: wrapper.trailingAnchor, constant: -12), stripScroll.topAnchor.constraint(equalTo: scriptGroup.bottomAnchor, constant: 9), stripScroll.bottomAnchor.constraint(equalTo: wrapper.bottomAnchor, constant: -11), stripDocument.heightAnchor.constraint(equalTo: stripScroll.contentView.heightAnchor), stripDocument.widthAnchor.constraint(greaterThanOrEqualTo: stripScroll.contentView.widthAnchor), clips.leadingAnchor.constraint(equalTo: stripDocument.leadingAnchor), clips.trailingAnchor.constraint(equalTo: stripDocument.trailingAnchor), clips.topAnchor.constraint(equalTo: stripDocument.topAnchor), clips.bottomAnchor.constraint(equalTo: stripDocument.bottomAnchor)])
        return wrapper
    }
    private func orderedClipCount(_ beat: StoryBeat) -> Int { beat.clips.count }
    private func makeClipInsertionZone(storyID: Int, beatID: Int, position: Int) -> DropZoneView {
        let zone = DropZoneView(label: "")
        zone.setBaseStyle(borderWidth: 0, cornerRadius: 3, backgroundColor: .clear)
        zone.onClip = { [weak self] id in self?.moveClip(id, story: storyID, targetBeat: beatID, position: position + 1) }
        NSLayoutConstraint.activate([zone.widthAnchor.constraint(equalToConstant: 12), zone.heightAnchor.constraint(equalToConstant: 94)])
        return zone
    }
    private func textField(_ value: String, font: NSFont, placeholder: String, lines: Int) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: value)
        field.font = font; field.isEditable = true; field.isSelectable = true; field.isBordered = false; field.drawsBackground = false; field.placeholderString = placeholder; field.maximumNumberOfLines = lines; field.translatesAutoresizingMaskIntoConstraints = false
        if lines == 0 {
            field.usesSingleLineMode = false; field.lineBreakMode = .byWordWrapping; field.cell?.wraps = true; field.cell?.isScrollable = true
        } else {
            field.lineBreakMode = .byTruncatingTail
        }
        return field
    }
    private func smallLabel(_ value: String) -> NSTextField { let f = NSTextField(labelWithString: value); f.font = .systemFont(ofSize: 10, weight: .semibold); f.textColor = .secondaryLabelColor; f.translatesAutoresizingMaskIntoConstraints = false; return f }
    private func makeSuggestionPanel(_ suggestion: StorySuggestion?) -> NSView {
        let wrap = NSView(); wrap.wantsLayer = true; wrap.layer?.backgroundColor = NSColor.systemPurple.withAlphaComponent(0.045).cgColor; wrap.layer?.cornerRadius = Theme.radiusPanel; wrap.layer?.borderColor = NSColor.systemPurple.withAlphaComponent(0.22).cgColor; wrap.layer?.borderWidth = 0.5
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 8; content.translatesAutoresizingMaskIntoConstraints = false
        let heading = NSStackView(); heading.orientation = .horizontal; heading.alignment = .centerY; heading.distribution = .fill; heading.spacing = 4
        let label = NSTextField(labelWithString: "✦  AI 建议"); label.font = .systemFont(ofSize: 12, weight: .semibold); label.textColor = .systemPurple
        let beta = NSTextField(labelWithString: "BETA"); beta.font = .systemFont(ofSize: 8.5, weight: .semibold); beta.textColor = .systemPurple; beta.wantsLayer = true; beta.layer?.backgroundColor = NSColor.systemPurple.withAlphaComponent(0.11).cgColor; beta.layer?.cornerRadius = 4; beta.alignment = .center; beta.widthAnchor.constraint(equalToConstant: 32).isActive = true; beta.heightAnchor.constraint(equalToConstant: 17).isActive = true
        heading.addArrangedSubview(label); heading.addArrangedSubview(beta); heading.setHuggingPriority(.required, for: .horizontal)
        content.addArrangedSubview(heading)
        if let suggestion {
            let title = NSTextField(wrappingLabelWithString: suggestion.title); title.font = .systemFont(ofSize: 13.5, weight: .semibold); title.maximumNumberOfLines = 2; content.addArrangedSubview(title)
            let reason = inspectorBody(suggestion.reason); reason.font = .systemFont(ofSize: 11.5); content.addArrangedSubview(reason)
            if !suggestion.suggestedIntent.isEmpty { content.addArrangedSubview(inspectorBlock("建议意图", suggestion.suggestedIntent)) }
            if !suggestion.suggestedScript.isEmpty { content.addArrangedSubview(inspectorBlock("建议脚本", suggestion.suggestedScript)) }
            let actions = NSStackView(); actions.orientation = .horizontal; actions.distribution = .fillEqually; actions.spacing = 7
            let apply = NSButton(title: "应用建议", target: self, action: #selector(applySuggestion(_:))); apply.tag = suggestion.id; Theme.applyPrimaryButton(apply)
            let dismiss = NSButton(title: "忽略", target: self, action: #selector(dismissSuggestion(_:))); dismiss.tag = suggestion.id; dismiss.bezelStyle = .rounded
            apply.heightAnchor.constraint(equalToConstant: 29).isActive = true; dismiss.heightAnchor.constraint(equalToConstant: 29).isActive = true
            actions.addArrangedSubview(apply); actions.addArrangedSubview(dismiss); content.addArrangedSubview(actions)
        } else {
            let failed = !storySuggestionError.isEmpty
            let title = NSTextField(labelWithString: failed ? "建议生成失败" : "等待编辑动作"); title.font = .systemFont(ofSize: 13, weight: .semibold); content.addArrangedSubview(title)
            content.addArrangedSubview(inspectorBody(failed ? storySuggestionError : "把素材拖到其他 Story Beat，Reelsift 会在这里解释叙事变化，并提出局部脚本建议。"))
        }
        wrap.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: wrap.topAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: wrap.bottomAnchor, constant: -12)])
        return wrap
    }

    private func currentStorySuggestion() -> StorySuggestion? {
        if let latestDragSuggestion, !resolvedSuggestionIDs.contains(latestDragSuggestion.id) {
            return latestDragSuggestion
        }
        guard let persisted = payload?.pendingSuggestion,
              !hiddenSuggestionIDs.contains(persisted.id),
              !resolvedSuggestionIDs.contains(persisted.id) else { return nil }
        return persisted
    }

    private func loadStoryChatHistory(storyID: Int) {
        storyChatLoadedForStoryID = storyID
        runBridge(["list-story-messages", String(storyID)], label: "正在读取对话记录…", completion: { [weak self] data in
            guard let self else { return }
            guard let envelope = try? JSONDecoder().decode(StoryChatMessagesEnvelope.self, from: data), envelope.ok, let messages = envelope.messages else { return }
            self.storyChatMessages = messages
            self.renderInspector()
        })
    }

    @objc private func toggleReasoningDisclosure(_ sender: NSButton) {
        let id = sender.tag
        if expandedReasoningMessageIDs.contains(id) { expandedReasoningMessageIDs.remove(id) } else { expandedReasoningMessageIDs.insert(id) }
        renderInspector()
    }

    private func makeInlineSuggestionCard(_ action: StoryChatAction) -> NSView {
        let card = NSView(); card.wantsLayer = true; card.layer?.backgroundColor = NSColor.systemPurple.withAlphaComponent(0.08).cgColor; card.layer?.cornerRadius = Theme.radiusCard; card.layer?.borderColor = NSColor.systemPurple.withAlphaComponent(0.25).cgColor; card.layer?.borderWidth = 0.5
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 5; content.translatesAutoresizingMaskIntoConstraints = false
        let heading = NSTextField(labelWithString: "✦ 建议改：\(action.beatTitle)"); heading.font = .systemFont(ofSize: 11, weight: .semibold); heading.textColor = .systemPurple; heading.maximumNumberOfLines = 1
        content.addArrangedSubview(heading)
        if !action.explanation.isEmpty { let explanation = inspectorBody(action.explanation); explanation.font = .systemFont(ofSize: 10.5); content.addArrangedSubview(explanation) }
        if !action.suggestedIntent.isEmpty { content.addArrangedSubview(inspectorBlock("建议意图", action.suggestedIntent)) }
        if !action.suggestedScript.isEmpty { content.addArrangedSubview(inspectorBlock("建议脚本", action.suggestedScript)) }
        if resolvedSuggestionIDs.contains(action.suggestionID) {
            let done = NSTextField(labelWithString: "已处理"); done.font = .systemFont(ofSize: 10, weight: .medium); done.textColor = .tertiaryLabelColor
            content.addArrangedSubview(done)
        } else {
            let actions = NSStackView(); actions.orientation = .horizontal; actions.distribution = .fillEqually; actions.spacing = 6
            let apply = NSButton(title: "应用建议", target: self, action: #selector(applySuggestion(_:))); apply.tag = action.suggestionID; Theme.applyPrimaryButton(apply); apply.heightAnchor.constraint(equalToConstant: 26).isActive = true
            let dismiss = NSButton(title: "忽略", target: self, action: #selector(dismissSuggestion(_:))); dismiss.tag = action.suggestionID; dismiss.bezelStyle = .rounded; dismiss.heightAnchor.constraint(equalToConstant: 26).isActive = true
            actions.addArrangedSubview(apply); actions.addArrangedSubview(dismiss)
            content.addArrangedSubview(actions)
        }
        card.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 8), content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -8), content.topAnchor.constraint(equalTo: card.topAnchor, constant: 7), content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -7)])
        return card
    }

    private func makeChatBubble(_ message: StoryChatMessage) -> NSView {
        let isUser = message.role == "user"
        let bubble = NSView(); bubble.wantsLayer = true
        bubble.layer?.backgroundColor = (isUser ? Theme.brandLime.withAlphaComponent(0.90) : NSColor.controlBackgroundColor).cgColor
        bubble.layer?.cornerRadius = Theme.radiusCard; bubble.layer?.borderWidth = 0.5; bubble.layer?.borderColor = (isUser ? Theme.brandLime : NSColor.separatorColor.withAlphaComponent(Theme.borderAlpha)).cgColor
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .leading; content.spacing = 5; content.translatesAutoresizingMaskIntoConstraints = false
        let text = NSTextField(wrappingLabelWithString: message.content.isEmpty ? "…" : message.content); text.font = Theme.body(); text.textColor = isUser ? Theme.brandForest : .labelColor; text.maximumNumberOfLines = 0
        content.addArrangedSubview(text)
        if !message.reasoningText.isEmpty {
            let expanded = expandedReasoningMessageIDs.contains(message.id)
            let toggle = NSButton(title: expanded ? "收起导演思考 ▲" : "查看导演思考 ▼", target: self, action: #selector(toggleReasoningDisclosure(_:)))
            toggle.tag = message.id; toggle.isBordered = false; toggle.font = .systemFont(ofSize: 10); toggle.contentTintColor = .secondaryLabelColor
            content.addArrangedSubview(toggle)
            if expanded {
                let reasoning = NSTextField(wrappingLabelWithString: message.reasoningText); reasoning.font = .systemFont(ofSize: 10.5); reasoning.textColor = .tertiaryLabelColor; reasoning.maximumNumberOfLines = 0
                content.addArrangedSubview(reasoning)
            }
        }
        if let action = message.action {
            content.addArrangedSubview(makeInlineSuggestionCard(action))
        }
        bubble.addSubview(content)
        bubble.widthAnchor.constraint(lessThanOrEqualToConstant: message.action != nil ? 280 : 230).isActive = true
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: bubble.leadingAnchor, constant: 10), content.trailingAnchor.constraint(equalTo: bubble.trailingAnchor, constant: -10), content.topAnchor.constraint(equalTo: bubble.topAnchor, constant: 7), content.bottomAnchor.constraint(equalTo: bubble.bottomAnchor, constant: -7)])
        let row = NSStackView(); row.orientation = .horizontal; row.alignment = .top; row.spacing = Theme.space2; row.translatesAutoresizingMaskIntoConstraints = false
        let spacer = NSView()
        let avatar = makeChatAvatar(isUser: isUser)
        if isUser { row.addArrangedSubview(spacer); row.addArrangedSubview(bubble); row.addArrangedSubview(avatar) } else { row.addArrangedSubview(avatar); row.addArrangedSubview(bubble); row.addArrangedSubview(spacer) }
        spacer.widthAnchor.constraint(greaterThanOrEqualToConstant: 12).isActive = true
        return row
    }

    private func makeAgentChatPanel(_ story: Story, pendingSuggestion: StorySuggestion?) -> NSView {
        let wrap = NSView(); Theme.applyCardStyle(wrap, cornerRadius: Theme.radiusPanel)
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 7; content.translatesAutoresizingMaskIntoConstraints = false
        let heading = NSTextField(labelWithString: "✦  导演 Agent"); heading.font = Theme.body(); heading.textColor = .systemPurple
        content.addArrangedSubview(heading)
        if storyChatLoadedForStoryID != story.id {
            loadStoryChatHistory(storyID: story.id)
            content.addArrangedSubview(inspectorBody("正在加载对话记录…"))
        } else {
            let scroll = NSScrollView(); scroll.borderType = .noBorder; scroll.drawsBackground = false; scroll.hasVerticalScroller = true; scroll.autohidesScrollers = false; scroll.translatesAutoresizingMaskIntoConstraints = false
            let messagesStack = NSStackView(); messagesStack.orientation = .vertical; messagesStack.alignment = .width; messagesStack.spacing = 8; messagesStack.translatesAutoresizingMaskIntoConstraints = false
            let flipped = FlippedView(); flipped.translatesAutoresizingMaskIntoConstraints = false; flipped.addSubview(messagesStack)
            // 拖拽产生的建议（move-clip / reorder-beats）没有对应的聊天消息，
            // 在这里合成一张"导演 Agent 卡片"，让它也出现在同一条时间线里。
            let representedSuggestionIDs = Set(storyChatMessages.compactMap { $0.action?.suggestionID })
            var dragSuggestionBubble: StoryChatMessage?
            if let suggestion = pendingSuggestion, !representedSuggestionIDs.contains(suggestion.id), !resolvedSuggestionIDs.contains(suggestion.id) {
                let action = StoryChatAction(suggestionID: suggestion.id, beatTitle: suggestion.title, explanation: suggestion.reason, suggestedIntent: suggestion.suggestedIntent, suggestedScript: suggestion.suggestedScript)
                dragSuggestionBubble = StoryChatMessage(id: -900_000 - suggestion.id, role: "assistant", content: "检测到你刚才拖动调整了素材/Beat 顺序，以下是我的建议：", action: action)
            }
            if storyChatMessages.isEmpty && dragSuggestionBubble == nil {
                messagesStack.addArrangedSubview(inspectorBody("还没有对话，可以先问问 AI 这批素材还能怎么讲，或者直接说想怎么改某个 Beat。"))
            } else {
                for message in storyChatMessages { messagesStack.addArrangedSubview(makeChatBubble(message)) }
                if let dragSuggestionBubble { messagesStack.addArrangedSubview(makeChatBubble(dragSuggestionBubble)) }
            }
            if storyChatBusy {
                let thinking = NSTextField(wrappingLabelWithString: "导演 Agent 正在构思回复…")
                thinking.font = .systemFont(ofSize: 11); thinking.textColor = .secondaryLabelColor
                messagesStack.addArrangedSubview(thinking)
            }
            scroll.documentView = flipped
            // 消息区是唯一可以吸收多余高度的区域，输入区因此稳定贴住卡片底部。
            scroll.setContentHuggingPriority(NSLayoutConstraint.Priority(rawValue: 1), for: .vertical); scroll.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
            messagesStack.setContentCompressionResistancePriority(.required, for: .vertical)
            NSLayoutConstraint.activate([scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 280), flipped.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor), flipped.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor), messagesStack.leadingAnchor.constraint(equalTo: flipped.leadingAnchor, constant: 2), messagesStack.trailingAnchor.constraint(equalTo: flipped.trailingAnchor, constant: -2), messagesStack.topAnchor.constraint(equalTo: flipped.topAnchor, constant: 2), messagesStack.bottomAnchor.constraint(equalTo: flipped.bottomAnchor, constant: -2)])
            content.addArrangedSubview(scroll)
        }
        let spacer = NSView(); spacer.setContentHuggingPriority(.defaultLow, for: .vertical); spacer.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        content.addArrangedSubview(spacer)
        storyChatInput.placeholderString = "跟导演 Agent 聊聊，或者说想怎么改某个 Beat…"
        storyChatInput.isEditable = !storyChatBusy; storyChatInput.isEnabled = !storyChatBusy; storyChatInput.isSelectable = true; storyChatInput.isBordered = true; storyChatInput.bezelStyle = .roundedBezel; storyChatInput.font = .systemFont(ofSize: 12); storyChatInput.target = self; storyChatInput.action = #selector(sendAgentChatMessage)
        storyChatInput.heightAnchor.constraint(equalToConstant: 26).isActive = true
        storyChatSendButton.title = "发送"; storyChatSendButton.target = self; storyChatSendButton.action = #selector(sendAgentChatMessage); storyChatSendButton.isEnabled = !storyChatBusy; Theme.applyPrimaryButton(storyChatSendButton)
        storyChatSpinner.style = .spinning; storyChatSpinner.controlSize = .small; storyChatSpinner.isDisplayedWhenStopped = false
        let inputRow = NSStackView(views: [storyChatInput, storyChatSendButton, storyChatSpinner]); inputRow.orientation = .horizontal; inputRow.alignment = .centerY; inputRow.spacing = 6; inputRow.translatesAutoresizingMaskIntoConstraints = false
        content.addArrangedSubview(inputRow)
        let hint = inspectorBody("只会改动你明确要求的那个 Beat，需要点“应用建议”才生效；整个故事重讲请用 Regenerate Story。"); hint.font = .systemFont(ofSize: 9.5)
        content.addArrangedSubview(hint)
        wrap.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: wrap.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: wrap.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: wrap.topAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: wrap.bottomAnchor, constant: -12)])
        return wrap
    }

    @objc private func sendAgentChatMessage() {
        guard let payload, let story = payload.story else { return }
        let text = storyChatInput.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        storyChatInput.stringValue = ""
        storyChatMessages.append(StoryChatMessage(id: -(storyChatMessages.count + 1), role: "user", content: text))
        storyChatBusy = true
        renderInspector()
        let args = ["agent-chat", folder.path, String(story.id), text, "--focused-beat-id", String(selectedBeatID ?? -1)]
        runBridge(args, label: "导演 Agent 正在思考…", onBusyChange: { [weak self] busy in
            guard let self else { return }
            self.storyChatBusy = busy
            self.storyChatSendButton.isEnabled = !busy
            self.storyChatInput.isEnabled = !busy
            self.storyChatSpinner.isHidden = !busy
            if busy { self.storyChatSpinner.startAnimation(nil) } else { self.storyChatSpinner.stopAnimation(nil) }
            self.renderInspector()
        }, completion: { [weak self] data in
            guard let self else { return }
            if let envelope = try? JSONDecoder().decode(AgentChatEnvelope.self, from: data), let message = envelope.message {
                self.storyChatMessages.append(message)
            } else if let envelope = try? JSONDecoder().decode(AgentChatEnvelope.self, from: data), let error = envelope.aiError {
                self.storyChatMessages.append(StoryChatMessage(id: -(self.storyChatMessages.count + 1), role: "assistant", content: error))
            }
            // 对话结果不再触发完整项目重载，避免把本地即时消息和输入状态覆盖掉。
            self.renderInspector()
        })
    }

    private func makeStoryOverview(_ story: Story, payload: DeskPayload) -> NSView {
        let card = NSView(); Theme.applyCardStyle(card, cornerRadius: Theme.radiusCard)
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 8; content.translatesAutoresizingMaskIntoConstraints = false
        let firstClipID = story.beats.sorted { $0.order < $1.order }.flatMap { $0.clips.sorted { $0.order < $1.order } }.first?.clipID
        let hero = NSImageView(); hero.wantsLayer = true; hero.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor; hero.layer?.cornerRadius = 6; hero.layer?.masksToBounds = true; hero.imageScaling = .scaleAxesIndependently; hero.image = firstClipID.flatMap { id in payload.items.first(where: { $0.id == id })?.coverPath }.flatMap(NSImage.init(contentsOfFile:)); hero.heightAnchor.constraint(equalToConstant: 96).isActive = true
        content.addArrangedSubview(hero)
        content.addArrangedSubview(inspectorTitle(story.title))
        content.addArrangedSubview(inspectorBody(story.thesis.isEmpty ? "尚未设置故事方向" : story.thesis))
        let used = Set(story.beats.flatMap { $0.clips.map(\.clipID) }).count
        let stats = NSStackView(); stats.orientation = .horizontal; stats.alignment = .top; stats.distribution = .fillEqually; stats.spacing = 0
        [("\(story.beats.count)", "BEATS"), ("\(used)", "USED"), ("\(max(0, payload.items.count - used))", "UNUSED")].forEach { value, label in
            let stat = NSStackView(); stat.orientation = .vertical; stat.alignment = .centerX; stat.spacing = 1
            let number = NSTextField(labelWithString: value); number.font = .monospacedDigitSystemFont(ofSize: 16, weight: .semibold); number.alignment = .center
            let caption = NSTextField(labelWithString: label); caption.font = .systemFont(ofSize: 9, weight: .semibold); caption.textColor = .secondaryLabelColor; caption.alignment = .center
            stat.addArrangedSubview(number); stat.addArrangedSubview(caption); stats.addArrangedSubview(stat)
        }
        content.addArrangedSubview(divider()); content.addArrangedSubview(stats)
        let preview = NSButton(title: "▶ 预览故事", target: self, action: #selector(previewStory)); Theme.applyPrimaryButton(preview); preview.alignment = .center; preview.heightAnchor.constraint(equalToConstant: 32).isActive = true; content.addArrangedSubview(preview)
        card.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: card.topAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -12)])
        return card
    }

    @objc private func showStoryMenu(_ sender: NSButton) { let menu = NSMenu(); let regenerate = NSMenuItem(title: "Regenerate Story", action: #selector(goToGenerateStoryStep), keyEquivalent: ""); regenerate.target = self; menu.addItem(regenerate); menu.popUp(positioning: regenerate, at: NSPoint(x: sender.bounds.maxX, y: sender.bounds.minY), in: sender) }

    private func makeInspectorTabs() -> NSSegmentedControl {
        let tabs = NSSegmentedControl(labels: ["故事", "片段"], trackingMode: .selectOne, target: self, action: #selector(changeInspectorMode(_:)))
        tabs.selectedSegment = inspectorMode == "clip" ? 1 : 0
        tabs.segmentStyle = .rounded
        tabs.controlSize = .small
        tabs.heightAnchor.constraint(equalToConstant: 28).isActive = true
        return tabs
    }

    @objc private func changeInspectorMode(_ sender: NSSegmentedControl) {
        flushPendingNote()
        inspectorMode = sender.selectedSegment == 1 ? "clip" : "story"
        renderInspector()
    }

    private func makeSelectedBeatSummary(_ beat: StoryBeat, story: Story) -> NSView {
        let card = NSView(); card.wantsLayer = true; card.layer?.backgroundColor = NSColor.selectedContentBackgroundColor.withAlphaComponent(0.055).cgColor; card.layer?.borderColor = NSColor.selectedContentBackgroundColor.withAlphaComponent(0.28).cgColor; card.layer?.borderWidth = 0.5; card.layer?.cornerRadius = 8
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 5; content.translatesAutoresizingMaskIntoConstraints = false
        content.addArrangedSubview(inspectorLabel("当前 STORY BEAT · \(beatNumber(beat.id, story: story))"))
        content.addArrangedSubview(inspectorTitle(beat.title))
        content.addArrangedSubview(inspectorBlock("意图", beat.intent))
        content.addArrangedSubview(inspectorMeta("\(beat.clips.count) 个素材"))
        content.addArrangedSubview(divider())
        content.addArrangedSubview(makeNoteEditor(title: "BEAT NOTE", caption: "这条备注会作为之后局部改写的创作上下文。", value: displayedBeatNote(beat), placeholder: "例如：这里要保留安静、没什么戏剧性的开始。", target: .beat(beat.id), height: 126))
        card.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: card.topAnchor, constant: 11), content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -11)])
        return card
    }

    private func makeEmptyClipInspector() -> NSView {
        let card = NSView(); card.wantsLayer = true; card.layer?.backgroundColor = NSColor.textBackgroundColor.cgColor; card.layer?.borderColor = NSColor.separatorColor.withAlphaComponent(0.8).cgColor; card.layer?.borderWidth = 0.5; card.layer?.cornerRadius = 8
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 6; content.translatesAutoresizingMaskIntoConstraints = false
        content.addArrangedSubview(inspectorLabel("CLIP"))
        let title = NSTextField(labelWithString: "选择一个素材"); title.font = .systemFont(ofSize: 15, weight: .semibold); content.addArrangedSubview(title)
        content.addArrangedSubview(inspectorBody("从左侧素材池或 Story Canvas 选择素材，在这里查看预览、视觉记忆与关联的 Story Beat。"))
        card.addSubview(content)
        NSLayoutConstraint.activate([content.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 12), content.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -12), content.topAnchor.constraint(equalTo: card.topAnchor, constant: 12), content.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -12)])
        return card
    }

    private func makeClipInspector(_ clip: DeskClip, payload: DeskPayload) -> NSView {
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = 8
        content.addArrangedSubview(inspectorLabel("CLIP")); content.addArrangedSubview(player); content.addArrangedSubview(inspectorTitle(clip.title))
        if let story = payload.story, let beat = story.beats.first(where: { $0.clips.contains(where: { $0.clipID == clip.id }) }) { content.addArrangedSubview(inspectorMeta("已用于  \(beatNumber(beat.id, story: story))  \(beat.title)")) } else { content.addArrangedSubview(inspectorMeta("尚未用于 Story")) }
        content.addArrangedSubview(inspectorBlock("AI VISUAL MEMORY", clip.summary))
        if !clip.transcript.isEmpty { content.addArrangedSubview(inspectorBlock("TRANSCRIPT", clip.transcript)) }
        content.addArrangedSubview(makeNoteEditor(title: "CREATOR NOTE", caption: "会与素材事实一起用于后续 Story 生成和局部建议。", value: displayedClipNote(clip), placeholder: "例如：这段不是在拍早餐，而是第一次觉得自己真的开始一个人生活了。", target: .clip(clip.id), height: 134))
        return content
    }

    private func makeReviewInspector(_ clip: DeskClip) -> NSView {
        let content = NSStackView(); content.orientation = .vertical; content.alignment = .width; content.spacing = Theme.space4
        let physicalInfo = makeReviewPhysicalInfo(clip); content.addArrangedSubview(physicalInfo); physicalInfo.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        let noteBox = NSView(); Theme.applyCardStyle(noteBox, cornerRadius: Theme.radiusPanel)
        let noteTitle = NSTextField(labelWithString: "我的随手记"); noteTitle.font = Theme.subhead(); noteTitle.translatesAutoresizingMaskIntoConstraints = false
        let optional = NSTextField(labelWithString: "自动保存"); optional.font = Theme.caption(); optional.textColor = Theme.brandMoss; optional.translatesAutoresizingMaskIntoConstraints = false
        let note = NSTextField(string: displayedClipNote(clip)); note.placeholderString = "例如：这段不是想拍早餐，而是想记住第一次觉得自己真的开始一个人生活了。"; note.font = .systemFont(ofSize: 12.5); note.textColor = .labelColor; note.isEditable = true; note.isSelectable = true; note.isBordered = true; note.bezelStyle = .roundedBezel; note.usesSingleLineMode = false; note.maximumNumberOfLines = 0; note.lineBreakMode = .byWordWrapping; note.cell?.wraps = true; note.cell?.isScrollable = true; note.delegate = self; note.translatesAutoresizingMaskIntoConstraints = false; reviewNoteBindings[ObjectIdentifier(note)] = clip.id
        let save = NSButton(title: "保存并下一条", target: self, action: #selector(saveReviewAndNext)); save.bezelStyle = .rounded
        let skip = NSButton(title: "跳过", target: self, action: #selector(skipReviewAndNext)); skip.bezelStyle = .rounded
        let actions = NSStackView(views: [save, NSView(), skip]); actions.orientation = .horizontal; actions.alignment = .centerY; actions.spacing = Theme.space2; actions.translatesAutoresizingMaskIntoConstraints = false
        let hint = NSTextField(wrappingLabelWithString: "这些备注会成为后续故事生成的创作上下文。"); hint.font = Theme.caption(); hint.textColor = .tertiaryLabelColor; hint.maximumNumberOfLines = 2; hint.translatesAutoresizingMaskIntoConstraints = false
        let next = NSButton(title: "下一步：生成故事 →", target: self, action: #selector(goToGenerateStory)); Theme.applyPrimaryButton(next); next.translatesAutoresizingMaskIntoConstraints = false
        noteBox.addSubview(noteTitle); noteBox.addSubview(optional); noteBox.addSubview(note); noteBox.addSubview(actions); noteBox.addSubview(hint); noteBox.addSubview(next)
        NSLayoutConstraint.activate([noteBox.heightAnchor.constraint(equalToConstant: 326), noteTitle.leadingAnchor.constraint(equalTo: noteBox.leadingAnchor, constant: Theme.space3), noteTitle.topAnchor.constraint(equalTo: noteBox.topAnchor, constant: Theme.space3), optional.trailingAnchor.constraint(equalTo: noteBox.trailingAnchor, constant: -Theme.space3), optional.centerYAnchor.constraint(equalTo: noteTitle.centerYAnchor), note.leadingAnchor.constraint(equalTo: noteBox.leadingAnchor, constant: Theme.space3), note.trailingAnchor.constraint(equalTo: noteBox.trailingAnchor, constant: -Theme.space3), note.topAnchor.constraint(equalTo: noteTitle.bottomAnchor, constant: Theme.space2), note.heightAnchor.constraint(equalToConstant: 128), actions.leadingAnchor.constraint(equalTo: noteBox.leadingAnchor, constant: Theme.space3), actions.trailingAnchor.constraint(equalTo: noteBox.trailingAnchor, constant: -Theme.space3), actions.topAnchor.constraint(equalTo: note.bottomAnchor, constant: Theme.space2), actions.heightAnchor.constraint(equalToConstant: 28), hint.leadingAnchor.constraint(equalTo: noteBox.leadingAnchor, constant: Theme.space3), hint.trailingAnchor.constraint(equalTo: noteBox.trailingAnchor, constant: -Theme.space3), hint.topAnchor.constraint(equalTo: actions.bottomAnchor, constant: Theme.space2), next.leadingAnchor.constraint(equalTo: noteBox.leadingAnchor, constant: Theme.space3), next.trailingAnchor.constraint(equalTo: noteBox.trailingAnchor, constant: -Theme.space3), next.bottomAnchor.constraint(equalTo: noteBox.bottomAnchor, constant: -Theme.space3), next.heightAnchor.constraint(equalToConstant: 32)])
        content.addArrangedSubview(noteBox); noteBox.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        return content
    }

    private func makeReviewPhysicalInfo(_ clip: DeskClip) -> NSView {
        let card = NSView(); card.wantsLayer = true
        let title = NSTextField(labelWithString: "视频物理信息"); title.font = Theme.subhead(); title.translatesAutoresizingMaskIntoConstraints = false
        let path = mediaPath(for: clip)
        let attributes = path.isEmpty ? nil : try? FileManager.default.attributesOfItem(atPath: path)
        let bytes = attributes?[.size] as? NSNumber
        let size = bytes.map { ByteCountFormatter.string(fromByteCount: $0.int64Value, countStyle: .file) } ?? "未知"
        let date = (attributes?[.creationDate] as? Date) ?? (attributes?[.modificationDate] as? Date)
        let formatter = DateFormatter(); formatter.locale = Locale(identifier: "zh_CN"); formatter.dateFormat = "yyyy-MM-dd HH:mm"
        let created = date.map(formatter.string) ?? "未知"
        let kind = URL(fileURLWithPath: path.isEmpty ? clip.filename : path).pathExtension.uppercased()
        let rows = [
            ("文件格式", kind.isEmpty ? "视频" : kind),
            ("文件大小", size),
            ("创建时间", created),
            ("拍摄设备", "未写入文件元数据"),
        ]
        let details = NSStackView(); details.orientation = .vertical; details.alignment = .width; details.spacing = Theme.space1; details.translatesAutoresizingMaskIntoConstraints = false
        for (name, value) in rows {
            let row = NSStackView(); row.orientation = .horizontal; row.alignment = .centerY; row.distribution = .fill; row.spacing = Theme.space2
            let label = NSTextField(labelWithString: name); label.font = Theme.small(); label.textColor = .secondaryLabelColor
            let text = NSTextField(labelWithString: value); text.font = Theme.small(); text.alignment = .right; text.lineBreakMode = .byTruncatingMiddle
            row.addArrangedSubview(label); row.addArrangedSubview(NSView()); row.addArrangedSubview(text)
            details.addArrangedSubview(row)
        }
        let rule = NSBox(); rule.boxType = .separator; rule.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(title); card.addSubview(details); card.addSubview(rule)
        NSLayoutConstraint.activate([card.heightAnchor.constraint(equalToConstant: 126), title.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: Theme.space1), title.topAnchor.constraint(equalTo: card.topAnchor), details.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: Theme.space1), details.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -Theme.space1), details.topAnchor.constraint(equalTo: title.bottomAnchor, constant: Theme.space2), rule.leadingAnchor.constraint(equalTo: card.leadingAnchor), rule.trailingAnchor.constraint(equalTo: card.trailingAnchor), rule.bottomAnchor.constraint(equalTo: card.bottomAnchor)])
        return card
    }

    private func renderInspector() {
        inspectorStack.arrangedSubviews.forEach { inspectorStack.removeArrangedSubview($0); $0.removeFromSuperview() }
        guard let payload else { return }
        noteBindings.removeAll(); reviewNoteBindings.removeAll(); noteStateLabels.removeAll()
        if workflowStep == 1 {
            if let clip = selectedReviewClip(in: payload) { let review = makeReviewInspector(clip); inspectorStack.addArrangedSubview(review); review.widthAnchor.constraint(equalTo: inspectorStack.widthAnchor).isActive = true }
            return
        }
        if workflowStep == 2 {
            addInspectorPanel(makeDirectionChatPanel(payload), fillsHeight: true)
            return
        }
        if workflowStep == 3 {
            if storyGenerationBusy {
                addInspectorPanel(makeStoryGenerationInspector(), fillsHeight: true)
            } else if let story = payload.story {
                addInspectorPanel(makeAgentChatPanel(story, pendingSuggestion: currentStorySuggestion()), fillsHeight: true)
            } else {
                inspectorStack.addArrangedSubview(inspectorLabel("STORY")); inspectorStack.addArrangedSubview(inspectorTitle("尚未生成 Story")); inspectorStack.addArrangedSubview(inspectorBody("先在 Generate Story 里确定方向。"))
            }
            return
        }
        inspectorStack.addArrangedSubview(makeInspectorTabs())
        if inspectorMode == "clip" {
            if let clipID = selectedClipID, let clip = payload.items.first(where: { $0.id == clipID }) {
                inspectorStack.addArrangedSubview(makeClipInspector(clip, payload: payload))
            } else {
                inspectorStack.addArrangedSubview(makeEmptyClipInspector())
            }
        } else if let story = payload.story {
            inspectorStack.addArrangedSubview(makeStoryOverview(story, payload: payload))
            if let beatID = selectedBeatID, let beat = story.beats.first(where: { $0.id == beatID }) {
                inspectorStack.addArrangedSubview(makeSelectedBeatSummary(beat, story: story))
            }
        } else {
            inspectorStack.addArrangedSubview(inspectorLabel("STORY")); inspectorStack.addArrangedSubview(inspectorTitle("尚未生成 Story")); inspectorStack.addArrangedSubview(inspectorBody("从中间的 Generate Story 开始。"))
        }
        inspectorStack.addArrangedSubview(divider()); inspectorStack.addArrangedSubview(makeSuggestionPanel(currentStorySuggestion()))
        inspectorStack.addArrangedSubview(makeFolderNote())
    }

    private func makeStoryGenerationInspector() -> NSView {
        let panel = NSView(); Theme.applyCardStyle(panel, cornerRadius: Theme.radiusPanel)
        let title = NSTextField(labelWithString: "✦  正在生成故事"); title.font = Theme.body(); title.textColor = .systemPurple; title.translatesAutoresizingMaskIntoConstraints = false
        let spinner = NSProgressIndicator(); spinner.style = .spinning; spinner.controlSize = .small; spinner.startAnimation(nil); spinner.translatesAutoresizingMaskIntoConstraints = false
        let detail = NSTextField(wrappingLabelWithString: "已选“\(pendingDirectionTitle.isEmpty ? "当前方向" : pendingDirectionTitle)”。生成会在后台继续；可随时切换到其他步骤，完成后这里会自动显示 Story Beat。")
        detail.font = Theme.body(); detail.textColor = .secondaryLabelColor; detail.maximumNumberOfLines = 0; detail.translatesAutoresizingMaskIntoConstraints = false
        panel.addSubview(title); panel.addSubview(spinner); panel.addSubview(detail)
        NSLayoutConstraint.activate([title.leadingAnchor.constraint(equalTo: panel.leadingAnchor, constant: 12), title.topAnchor.constraint(equalTo: panel.topAnchor, constant: 12), spinner.trailingAnchor.constraint(equalTo: panel.trailingAnchor, constant: -12), spinner.centerYAnchor.constraint(equalTo: title.centerYAnchor), detail.leadingAnchor.constraint(equalTo: title.leadingAnchor), detail.trailingAnchor.constraint(equalTo: spinner.trailingAnchor), detail.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 10), detail.bottomAnchor.constraint(lessThanOrEqualTo: panel.bottomAnchor, constant: -12)])
        return panel
    }
    private func inspectorLabel(_ value: String) -> NSTextField { let field = NSTextField(labelWithString: value); field.font = .systemFont(ofSize: 10, weight: .semibold); field.textColor = .secondaryLabelColor; return field }
    private func inspectorTitle(_ value: String) -> NSTextField { let field = NSTextField(wrappingLabelWithString: value); field.font = .systemFont(ofSize: 16, weight: .semibold); field.maximumNumberOfLines = 3; return field }
    private func inspectorBody(_ value: String) -> NSTextField { let field = NSTextField(wrappingLabelWithString: value); field.font = .systemFont(ofSize: 12); field.textColor = .secondaryLabelColor; field.maximumNumberOfLines = 0; return field }
    private func inspectorMeta(_ value: String) -> NSTextField { let field = inspectorBody(value); field.font = .systemFont(ofSize: 11, weight: .medium); return field }
    private func inspectorBlock(_ heading: String, _ value: String) -> NSView { let stack = NSStackView(); stack.orientation = .vertical; stack.alignment = .width; stack.spacing = 4; stack.addArrangedSubview(inspectorLabel(heading)); stack.addArrangedSubview(inspectorBody(value.isEmpty ? "—" : value)); return stack }
    private func beatNumber(_ beatID: Int, story: Story?) -> String { guard let story, let index = story.beats.sorted(by: { $0.order < $1.order }).firstIndex(where: { $0.id == beatID }) else { return "—" }; return String(format: "%02d", index + 1) }

    @objc private func generateStory() { runBridge(["generate-story", folder.path], label: "正在从已识别素材生成 Story Beats…", completion: { [weak self] data in guard let self else { return }; self.workflowStep = 3; self.applyBridgePayload(data) }) }
    @objc private func goToGenerateStoryStep() { selectWorkflowStep(2) }
    @objc private func generateDirections() {
        let typed = storyBriefField.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if !typed.isEmpty { directionChatMessages.append((role: "user", text: typed)); storyBriefField.string = "" }
        let args = ["generate-directions", folder.path, "--theme", currentDirectionBrief(), "--duration", "60"]
        runBridge(args, label: "正在生成 3 个故事方向（标题 + 风格 + 结构）…", onBusyChange: { [weak self] busy in
            guard let self else { return }
            self.directionsBusy = busy
            self.generateDirectionsButton.isEnabled = !busy
            self.storyBriefField.isEditable = !busy
            self.generateDirectionsSpinner.isHidden = !busy
            if busy { self.generateDirectionsSpinner.startAnimation(nil) } else { self.generateDirectionsSpinner.stopAnimation(nil) }
        }) { [weak self] data in
            guard let self else { return }
            guard let result = try? JSONDecoder().decode(StoryDirectionsEnvelope.self, from: data) else { self.showBridgeError("Story Directions 返回格式无法读取"); return }
            guard result.ok, let directions = result.directions, directions.count == 3 else { self.showBridgeError(result.error ?? "没有生成三个可用方向"); return }
            self.storyDirections = directions
            self.directionChatMessages.append((role: "assistant", text: "已生成 3 个新方向，可以在中间查看并选择，或者继续在这里补充想法重新生成。"))
            self.rebuildCanvas(); self.renderInspector(); self.statusLabel.stringValue = "已生成 3 个故事方向，请选择一个"
        }
    }
    @objc private func chooseDirection(_ sender: NSButton) {
        guard storyDirections.indices.contains(sender.tag), let data = try? JSONEncoder().encode(storyDirections[sender.tag]), let json = String(data: data, encoding: .utf8) else { return }
        let args = ["choose-direction", folder.path, json, "--theme", currentDirectionBrief(), "--duration", "60"]
        let originalTitle = sender.title
        pendingDirectionTitle = storyDirections[sender.tag].title
        storyGenerationBusy = true
        workflowStep = 3
        rebuildInterface()
        runBridge(args, label: "正在把选中方向生成 Story Beats…", onBusyChange: { [weak self] busy in
            sender.isEnabled = !busy
            sender.title = busy ? "生成中…" : originalTitle
            if !busy {
                self?.storyGenerationBusy = false
                self?.rebuildInterface()
            }
        }) { [weak self] data in
            guard let self else { return }
            self.storyGenerationBusy = false
            self.applyBridgePayload(data)
            self.selectedDirectionStoryID = self.payload?.story?.id
            self.workflowStep = 3
            self.rebuildInterface()
        }
    }
    private func applyStoryMoveResult(_ data: Data, revision: Int) {
        guard revision == storyMoveRevision else { return }
        guard let envelope = try? JSONDecoder().decode(StoryMoveEnvelope.self, from: data) else {
            applyBridgePayload(data)
            return
        }
        applyBridgePayload(data)
        if let request = envelope.interpretationRequest {
            scheduleStoryMoveInterpretation(request, revision: revision)
        }
    }
    private func scheduleStoryMoveInterpretation(_ request: StoryInterpretationRequest, revision: Int) {
        guard let requestData = try? JSONEncoder().encode(request),
              let requestJSON = String(data: requestData, encoding: .utf8) else {
            statusLabel.stringValue = "素材顺序已保存，但无法构造后台故事建议。"
            return
        }
        runBridge(["interpret-move", folder.path, requestJSON], label: "素材顺序已保存，正在后台整理故事建议…", completion: { [weak self] data in
            guard let self, revision == self.storyMoveRevision else { return }
            self.applyStoryMoveInterpretationResult(data)
        })
    }
    private func applyStoryMoveInterpretationResult(_ data: Data) {
        guard let envelope = try? JSONDecoder().decode(StoryMoveEnvelope.self, from: data) else {
            showBridgeError("后台故事建议返回格式无法读取")
            return
        }
        if let aiError = envelope.aiError {
            storySuggestionError = aiError
            statusLabel.stringValue = aiError
            return
        }
        if envelope.stale == true {
            statusLabel.stringValue = envelope.interpretation ?? "素材位置已再次变化，已丢弃较早的 AI 建议。"
            return
        }
        guard let suggestion = envelope.suggestion else {
            statusLabel.stringValue = envelope.interpretation ?? "素材顺序已保存"
            return
        }
        if !storyChatMessages.contains(where: { $0.action?.suggestionID == suggestion.id }) {
            let action = StoryChatAction(suggestionID: suggestion.id, beatTitle: suggestion.title, explanation: suggestion.reason, suggestedIntent: suggestion.suggestedIntent, suggestedScript: suggestion.suggestedScript)
            storyChatMessages.append(StoryChatMessage(id: -900_000 - suggestion.id, role: "assistant", content: "我根据这次拖动整理了一条局部修改建议：", action: action))
        }
        latestDragSuggestion = suggestion
        hiddenSuggestionIDs.remove(suggestion.id)
        statusLabel.stringValue = "已生成一条可选的故事建议"
        renderInspector()
    }
    private func moveClip(_ id: Int, story: Int, targetBeat: Int, position: Int?) {
        if let suggestion = currentStorySuggestion() { hiddenSuggestionIDs.insert(suggestion.id) }
        latestDragSuggestion = nil
        storyMoveRevision += 1
        let revision = storyMoveRevision
        var args = ["move-clip", folder.path, String(story), String(id), String(targetBeat)]
        if let p = position { args += ["--position", String(p)] }
        runBridge(args, label: "正在保存素材位置…", completion: { [weak self] in self?.applyStoryMoveResult($0, revision: revision) })
    }
    private func moveBeat(_ id: Int, story: Story, toIndex: Int) {
        var ids = story.beats.sorted { $0.order < $1.order }.map(\.id)
        guard let old = ids.firstIndex(of: id) else { return }
        ids.remove(at: old); ids.insert(id, at: max(0, min(toIndex, ids.count)))
        guard let data = try? JSONSerialization.data(withJSONObject: ids), let json = String(data: data, encoding: .utf8) else { return }
        if let suggestion = currentStorySuggestion() { hiddenSuggestionIDs.insert(suggestion.id) }
        latestDragSuggestion = nil
        storyMoveRevision += 1
        let revision = storyMoveRevision
        runBridge(["reorder-beats", folder.path, String(story.id), json], label: "正在保存 Story Beat 顺序…", completion: { [weak self] in self?.applyStoryMoveResult($0, revision: revision) })
    }
    private func bind(_ field: NSTextField, beatID: Int, key: String) { field.target = self; field.action = #selector(commitBeatEdit(_:)); fieldBindings[ObjectIdentifier(field)] = (beatID, key) }
    @objc private func commitBeatEdit(_ sender: NSTextField) { guard let payload, let story = payload.story, let binding = fieldBindings[ObjectIdentifier(sender)], let beat = story.beats.first(where: { $0.id == binding.0 }) else { return }; let flag = binding.1 == "title" ? "--title" : (binding.1 == "intent" ? "--intent" : "--script"); runBridge(["update-beat", String(beat.id), flag, sender.stringValue], label: "正在保存 Beat…", completion: { [weak self] in self?.applyBridgePayload($0) }) }
    @objc private func applySuggestion(_ sender: NSButton) { let id = sender.tag; runBridge(["apply-suggestion", String(id)], label: "正在应用建议…", completion: { [weak self] data in guard let self else { return }; self.resolvedSuggestionIDs.insert(id); if self.latestDragSuggestion?.id == id { self.latestDragSuggestion = nil }; self.applyBridgePayload(data) }) }
    @objc private func dismissSuggestion(_ sender: NSButton) { let id = sender.tag; runBridge(["dismiss-suggestion", String(id)], label: "已忽略这条建议", completion: { [weak self] data in guard let self else { return }; self.resolvedSuggestionIDs.insert(id); if self.latestDragSuggestion?.id == id { self.latestDragSuggestion = nil }; self.applyBridgePayload(data) }) }
    @objc private func changeWorkflowStep(_ sender: NSButton) { selectWorkflowStep(sender.tag) }
    @objc private func goToGenerateStory() { selectWorkflowStep(2) }
    @objc private func skipRemainingReviewClips() {
        guard let payload else { return }
        let pending = payload.items.filter { !isReviewed($0) }
        guard !pending.isEmpty else { statusLabel.stringValue = "所有素材均已查看"; return }
        pending.forEach { clip in
            reviewStatusDrafts[clip.id] = "skipped"
            runBridge(["save-clip-note-status", String(clip.id), "skipped"], label: "正在跳过剩余素材…")
        }
        updateSidebarSummary(); footageTable.reloadData(); rebuildCanvas(); renderInspector()
        statusLabel.stringValue = "已跳过剩余素材"
    }
    private func selectWorkflowStep(_ step: Int) {
        flushPendingNote()
        let requestedStep = max(1, min(step, 4))
        if requestedStep >= 3, !storyGenerationBusy, selectedDirectionStoryID != payload?.story?.id {
            workflowStep = 2
            statusLabel.stringValue = "请先在第 2 步选择一个故事方向，再进入编排故事。"
            updateSidebarSummary(); footageTable.reloadData(); rebuildCanvas(); renderInspector(); updateFlowStepper()
            return
        }
        workflowStep = requestedStep
        if workflowStep == 1, selectedClipID == nil { selectedClipID = payload?.items.first?.id }
        updateSidebarSummary(); footageTable.reloadData(); rebuildCanvas(); renderInspector(); updateFlowStepper()
    }
    @objc private func changeFilter(_ sender: NSButton) { filter = sender.identifier?.rawValue ?? "all"; updateSidebarSummary(); footageTable.reloadData() }
    private func visibleClips() -> [DeskClip] { guard let payload else { return [] }; switch filter { case "reviewed": return payload.items.filter { isReviewed($0) }; case "notes": return payload.items.filter { hasCreatorNote($0) }; default: return payload.items } }
    func numberOfRows(in tableView: NSTableView) -> Int { visibleClips().count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? { let clips = visibleClips(); guard row < clips.count else { return nil }; let used = Set(payload?.story?.beats.flatMap { $0.clips.map(\.clipID) } ?? []); let clip = clips[row]; let cell = NSTableCellView(); let metadata = workflowStep == 1 ? (isReviewed(clip) ? "已查看" : "未查看") : (used.contains(clip.id) ? "已使用" : "未使用"); let card = ClipCardView(clip: clip, status: metadata, hasCreatorNote: hasCreatorNote(clip), selected: selectedClipID == clip.id, select: { [weak self] in self?.showClipFromSidebar(clip) }); card.translatesAutoresizingMaskIntoConstraints = false; cell.addSubview(card); NSLayoutConstraint.activate([card.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 2), card.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -2), card.topAnchor.constraint(equalTo: cell.topAnchor, constant: 2), card.bottomAnchor.constraint(equalTo: cell.bottomAnchor, constant: -2)]); return cell }
    func tableViewSelectionDidChange(_ notification: Notification) { let clips = visibleClips(); if footageTable.selectedRow >= 0 && footageTable.selectedRow < clips.count { showClipFromSidebar(clips[footageTable.selectedRow]) } }
    private func showClipFromSidebar(_ clip: DeskClip) {
        if workflowStep != 1 { workflowStep = 1; updateSidebarSummary(); updateFlowStepper() }
        showClip(clip)
    }
    private func mediaPath(for clip: DeskClip) -> String {
        if !clip.previewPath.isEmpty && FileManager.default.fileExists(atPath: clip.previewPath) { return clip.previewPath }
        return clip.filepath
    }
    private func showClip(_ clip: DeskClip) { flushPendingNote(); inspectorMode = "clip"; selectedClipID = clip.id; selectedBeatID = nil; let path = mediaPath(for: clip); if workflowStep == 1 { setReviewPlayer(clip) } else if !path.isEmpty { player.player = AVPlayer(url: URL(fileURLWithPath: path)) }; footageTable.reloadData(); rebuildCanvas(); renderInspector() }
    private func showBeat(_ beatID: Int) { flushPendingNote(); inspectorMode = "story"; selectedBeatID = beatID; selectedClipID = nil; footageTable.reloadData(); rebuildCanvas(); renderInspector() }
    @objc private func previewStory() { guard let payload, let story = payload.story else { statusLabel.stringValue = "先生成 Story 后再预览"; return }; workflowStep = 4; let links = story.beats.sorted { $0.order < $1.order }.flatMap { beat in beat.clips.sorted { $0.order < $1.order }.map { (beat.title, $0) } }; let pairs = links.compactMap { beatTitle, link -> (AVPlayerItem, Double?, Double?, String, String)? in guard let clip = payload.items.first(where: { $0.id == link.clipID }) else { return nil }; let path = mediaPath(for: clip); guard !path.isEmpty, FileManager.default.fileExists(atPath: path) else { return nil }; return (AVPlayerItem(url: URL(fileURLWithPath: path)), link.inPoint, link.outPoint, beatTitle, clip.filename) }; guard !pairs.isEmpty else { statusLabel.stringValue = "Story Canvas 尚无可播放素材"; return }; if let firstLink = links.first, let firstClip = payload.items.first(where: { $0.id == firstLink.1.clipID }) { selectedClipID = firstClip.id; selectedBeatID = nil }; updateSidebarSummary(); footageTable.reloadData(); rebuildCanvas(); renderInspector(); updateFlowStepper(); previewQueue?.pause(); if let observer = previewObserver, let old = previewQueue { old.removeTimeObserver(observer) }; let queue = AVQueuePlayer(items: pairs.map { $0.0 }); previewQueue = queue; previewRanges = pairs.map { ($0.1, $0.2) }; previewLabels = pairs.map { ($0.3, $0.4) }; previewRangeIndex = 0; previewActiveItem = queue.currentItem; reviewPlayer.player = queue; previewItemObservation = queue.observe(\.currentItem, options: [.new]) { [weak self] _, _ in self?.handlePreviewItemChange() }; previewObserver = queue.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.2, preferredTimescale: 600), queue: .main) { [weak self] time in self?.advancePreviewIfNeeded(time) }; statusLabel.stringValue = "正在按当前 Story Canvas 预览"; updatePreviewLabel(); seekPreviewStart(); queue.play() }
    private func handlePreviewItemChange() { guard let item = previewQueue?.currentItem else { return }; if previewActiveItem != nil && previewActiveItem !== item { previewRangeIndex += 1 }; previewActiveItem = item; updatePreviewLabel(); seekPreviewStart() }
    private func updatePreviewLabel() { guard previewRangeIndex < previewLabels.count else { return }; inspectorMode = "clip"; renderInspector(); let current = previewLabels[previewRangeIndex]; statusLabel.stringValue = "正在预览：\(current.beat) · \(current.clip)" }
    private func currentPreviewRange() -> (Double?, Double?)? { guard previewRangeIndex < previewRanges.count else { return nil }; return previewRanges[previewRangeIndex] }
    private func seekPreviewStart() { guard let queue = previewQueue, let range = currentPreviewRange(), let start = range.0 else { return }; queue.seek(to: CMTime(seconds: start, preferredTimescale: 600)) }
    private func advancePreviewIfNeeded(_ time: CMTime) { guard let queue = previewQueue, let range = currentPreviewRange(), let end = range.1, time.seconds >= end else { return }; queue.advanceToNextItem(); seekPreviewStart() }
    func textDidChange(_ notification: Notification) {
        guard let textView = notification.object as? NSTextView else { return }
        if let clipID = reviewNoteBindings[ObjectIdentifier(textView)] {
            clipNoteDrafts[clipID] = textView.string
            return
        }
        guard let target = noteBindings[ObjectIdentifier(textView)] else { return }
        let value = textView.string
        switch target {
        case .clip(let clipID): clipNoteDrafts[clipID] = value
        case .beat(let beatID): beatNoteDrafts[beatID] = value
        case .folder: break
        }
        noteSaveTimer?.invalidate()
        pendingNoteTarget = target
        pendingNoteText = value
        noteStateLabels[ObjectIdentifier(textView)]?.stringValue = "等待保存…"
        statusLabel.stringValue = "\(target.displayName)将在停止输入后保存"
        noteSaveTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: false) { [weak self] _ in self?.flushPendingNote() }
        if case .clip = target { updateSidebarSummary(); footageTable.reloadData() }
    }

    func controlTextDidChange(_ notification: Notification) {
        guard let field = notification.object as? NSTextField,
              let clipID = reviewNoteBindings[ObjectIdentifier(field)] else { return }
        let target = NoteTarget.clip(clipID)
        clipNoteDrafts[clipID] = field.stringValue
        noteSaveTimer?.invalidate()
        pendingNoteTarget = target
        pendingNoteText = field.stringValue
        statusLabel.stringValue = "\(target.displayName)将在停止输入后保存"
        noteSaveTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: false) { [weak self] _ in self?.flushPendingNote() }
    }

    @objc private func saveReviewAndNext() {
        guard let payload, let clip = selectedReviewClip(in: payload) else { return }
        flushPendingNote()
        let note = displayedClipNote(clip)
        statusLabel.stringValue = "正在保存随手记…"
        runBridge(["save-clip-note", String(clip.id), note], label: "正在保存随手记…", completion: { [weak self] _ in
            guard let self else { return }
            self.persistReviewStatus(clipID: clip.id, status: "done")
        })
    }

    @objc private func skipReviewAndNext() {
        guard let payload, let clip = selectedReviewClip(in: payload) else { return }
        flushPendingNote()
        persistReviewStatus(clipID: clip.id, status: "skipped")
    }

    private func persistReviewStatus(clipID: Int, status: String) {
        reviewStatusDrafts[clipID] = status
        updateSidebarSummary(); footageTable.reloadData()
        runBridge(["save-clip-note-status", String(clipID), status], label: "正在保存查看进度…", completion: { [weak self] _ in
            guard let self else { return }
            self.advanceReview(after: clipID)
        })
    }

    private func advanceReview(after clipID: Int) {
        guard let payload else { return }
        let clips = payload.items
        guard let index = clips.firstIndex(where: { $0.id == clipID }) else { return }
        if index + 1 < clips.count {
            showClip(clips[index + 1])
        } else {
            rebuildCanvas(); renderInspector(); statusLabel.stringValue = "已到最后一条素材；可以继续生成 Story"
        }
    }

    private func flushPendingNote() {
        noteSaveTimer?.invalidate(); noteSaveTimer = nil
        guard let target = pendingNoteTarget else { return }
        let value = pendingNoteText
        pendingNoteTarget = nil; pendingNoteText = ""
        let args: [String]
        switch target {
        case .folder(let libraryID): args = ["save-library-note", String(libraryID), value]
        case .clip(let clipID): args = ["save-clip-note", String(clipID), value]
        case .beat(let beatID): args = ["update-beat", String(beatID), "--note", value]
        }
        statusLabel.stringValue = "正在保存\(target.displayName)…"
        runBridge(args, label: "正在保存\(target.displayName)…", completion: { [weak self] _ in
            guard let self else { return }
            self.statusLabel.stringValue = "\(target.displayName)已保存到本地"
            for (viewID, boundTarget) in self.noteBindings where boundTarget == target {
                self.noteStateLabels[viewID]?.stringValue = "已保存"
            }
        })
    }

    func windowWillClose(_ notification: Notification) {
        previewQueue?.pause()
        flushPendingNote()
        if let payload, pendingNoteTarget == nil { runBridge(["save-library-note", String(payload.libraryID), folderNote.string], label: "正在保存项目随手记…") }
    }
}

/// 展示 Finder 整理任务的实时状态，避免长时间 AI 分析时看起来像没有响应。
final class OrganizerProgressController: NSWindowController {
    private let progress = NSProgressIndicator()
    private let titleLabel = NSTextField(labelWithString: "正在准备素材…")
    private let detailLabel = NSTextField(labelWithString: "")
    private let openButton = NSButton(title: "在 Finder 中打开", target: nil, action: nil)
    private let folder: URL
    private let startedAt = Date()
    private var completedCount = 0
    var onClose: (() -> Void)?

    init(folder: URL) {
        self.folder = folder
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 174), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Reelsift 正在整理素材"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self

        let content = NSView()
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = content
        titleLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingMiddle
        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.lineBreakMode = .byTruncatingMiddle
        progress.minValue = 0; progress.maxValue = 100; progress.doubleValue = 0; progress.isIndeterminate = false
        progress.controlSize = .regular
        openButton.isHidden = true
        openButton.target = self
        openButton.action = #selector(openResultFolder)
        [titleLabel, detailLabel, progress, openButton].forEach { view in view.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(view) }
        NSLayoutConstraint.activate([
            titleLabel.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24), titleLabel.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24), titleLabel.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            detailLabel.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor), detailLabel.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor), detailLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 8),
            progress.leadingAnchor.constraint(equalTo: titleLabel.leadingAnchor), progress.trailingAnchor.constraint(equalTo: titleLabel.trailingAnchor), progress.topAnchor.constraint(equalTo: detailLabel.bottomAnchor, constant: 18),
            openButton.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24), openButton.topAnchor.constraint(equalTo: progress.bottomAnchor, constant: 18),
        ])
    }

    required init?(coder: NSCoder) { nil }

    func show() { window?.center(); window?.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }

    func update(with event: [String: Any]) {
        let stage = event["stage"] as? String ?? ""
        let index = event["index"] as? Int ?? 0
        let total = max(1, event["total"] as? Int ?? 1)
        let filename = event["filename"] as? String ?? ""
        let detail = event["detail"] as? String ?? ""
        let completedStages: Set<String> = ["named", "indexing", "organizing", "completed"]
        let fraction = completedStages.contains(stage) ? Double(index) / Double(total) : Double(max(0, index - 1)) / Double(total)
        progress.doubleValue = min(100, max(0, fraction * 100))
        if stage == "named" { completedCount = max(completedCount, index) }
        titleLabel.stringValue = detail.isEmpty ? "正在处理 \(filename)" : detail
        detailLabel.stringValue = "\(min(index, total)) / \(total) · \(filename) · \(estimatedRemainingText(total: total, stage: stage))"
        if stage == "completed" { finish(success: true, message: "已完成 \(index) / \(total) 个视频") }
    }

    private func estimatedRemainingText(total: Int, stage: String) -> String {
        if stage == "completed" { return "已完成" }
        if stage == "indexing" || stage == "organizing" { return "正在收尾" }
        guard completedCount >= 2 else { return "正在估算剩余时间" }
        let secondsPerVideo = Date().timeIntervalSince(startedAt) / Double(completedCount)
        let remainingSeconds = secondsPerVideo * Double(max(0, total - completedCount))
        if remainingSeconds < 60 { return "预计剩余不足 1 分钟" }
        let minutes = Int(remainingSeconds) / 60
        let seconds = Int(remainingSeconds) % 60
        return seconds == 0 ? "预计剩余约 \(minutes) 分钟" : "预计剩余约 \(minutes) 分 \(seconds) 秒"
    }

    func finish(success: Bool, message: String) {
        progress.doubleValue = success ? 100 : progress.doubleValue
        titleLabel.stringValue = success ? "素材整理完成" : "素材整理失败"
        detailLabel.stringValue = message
        openButton.isHidden = !success
    }

    @objc private func openResultFolder() { NSWorkspace.shared.open(folder) }
}

extension OrganizerProgressController: NSWindowDelegate {
    func windowWillClose(_ notification: Notification) { onClose?() }
}

final class FinderServiceProvider: NSObject {
    private var desks: [DirectorDeskController] = []
    private var organizerWindows: [OrganizerProgressController] = []

    @objc(runWorkflowAsService:userData:error:)
    func runWorkflowAsService(_ pasteboard: NSPasteboard, userData: String?, error: AutoreleasingUnsafeMutablePointer<NSString?>) { handle(pasteboard, error: error, mode: "organize") }
    @objc(openDirectorDesk:userData:error:)
    func openDirectorDesk(_ pasteboard: NSPasteboard, userData: String?, error: AutoreleasingUnsafeMutablePointer<NSString?>) { handle(pasteboard, error: error, mode: "desk") }

    private func handle(_ pasteboard: NSPasteboard, error: AutoreleasingUnsafeMutablePointer<NSString?>, mode: String) {
        let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        guard let folder = urls.first, folder.hasDirectoryPath else { error.pointee = "请先在 Finder 中选中一个文件夹。"; return }
        DispatchQueue.main.async { if mode == "desk" { let desk = DirectorDeskController(folder: folder); self.desks.append(desk); desk.show() } else { self.runOrganizer(folder) } }
    }

    private func runOrganizer(_ folder: URL) {
        let alert = NSAlert(); alert.messageText = "Reelsift AI 整理素材"; alert.informativeText = "分析后会复制到同级新文件夹，原素材不变。"; alert.addButton(withTitle: "保存到新文件夹"); alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let controller = OrganizerProgressController(folder: folder.deletingLastPathComponent())
        organizerWindows.append(controller)
        controller.onClose = { [weak self, weak controller] in guard let controller else { return }; self?.organizerWindows.removeAll { $0 === controller } }
        controller.show()
        let process = Process(); let output = Pipe(); let errors = Pipe()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [organizerPath, folder.path, "--mode", "copy", "--progress-json"]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDirectory)
        process.standardOutput = output; process.standardError = errors
        output.fileHandleForReading.readabilityHandler = { [weak controller] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            for line in String(decoding: data, as: UTF8.self).split(separator: "\n") {
                guard let event = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { continue }
                DispatchQueue.main.async { controller?.update(with: event) }
            }
        }
        process.terminationHandler = { [weak controller] completed in
            output.fileHandleForReading.readabilityHandler = nil
            let errorText = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async { if completed.terminationStatus != 0 { controller?.finish(success: false, message: errorText.isEmpty ? "请查看 data/finder-organize.log" : errorText) } }
        }
        do { try process.run() } catch { controller.finish(success: false, message: error.localizedDescription) }
    }
}
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let provider = FinderServiceProvider()
    private var launchedDesk: DirectorDeskController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let launchFolder = folderFromLaunchArguments()
        NSApp.setActivationPolicy(launchFolder == nil ? .accessory : .regular)
        NSRegisterServicesProvider(provider, "com.reelsift.finder-service")
        guard let folder = launchFolder else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let desk = DirectorDeskController(folder: folder)
            self.launchedDesk = desk
            desk.show()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        launchedDesk?.show()
        return true
    }

    private func folderFromLaunchArguments() -> URL? {
        guard let option = CommandLine.arguments.firstIndex(of: "--open-folder"),
              CommandLine.arguments.indices.contains(option + 1) else { return nil }
        let folder = URL(fileURLWithPath: CommandLine.arguments[option + 1]).standardizedFileURL
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: folder.path, isDirectory: &isDirectory), isDirectory.boolValue else { return nil }
        return folder
    }
}
let app = NSApplication.shared; let delegate = AppDelegate(); app.delegate = delegate; app.run()
