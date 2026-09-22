// Reelsift Finder 动作的原生弹窗：仅负责选择模式和展示完成结果。
import AppKit
import Foundation

func showAlert(_ title: String, _ message: String, _ buttons: [String]) -> NSApplication.ModalResponse {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.activate(ignoringOtherApps: true)
    let alert = NSAlert()
    alert.messageText = title
    alert.informativeText = message
    alert.alertStyle = .informational
    for button in buttons { alert.addButton(withTitle: button) }
    return alert.runModal()
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else { exit(1) }
let mode = arguments[1]

switch mode {
case "choose":
    let folder = arguments.count > 2 ? arguments[2] : "所选文件夹"
    let count = arguments.count > 3 ? arguments[3] : "0"
    let response = showAlert(
        "Reelsift AI 整理素材",
        "将在“\(folder)”中分析 \(count) 个视频。\n\n默认会复制到同级的新文件夹，原素材不会变化；也可以直接重命名原文件。",
        ["保存到新文件夹", "重命名原文件", "取消"]
    )
    print(response == .alertFirstButtonReturn ? "copy" : response == .alertSecondButtonReturn ? "rename" : "cancel")
case "confirm_rename":
    let folder = arguments.count > 2 ? arguments[2] : "所选文件夹"
    let response = showAlert(
        "确认直接重命名？",
        "这会修改“\(folder)”内分析成功的视频文件名。Reelsift 会生成 CSV 清单，但不会保留一份副本。",
        ["仍要重命名", "取消"]
    )
    print(response == .alertFirstButtonReturn ? "rename" : "cancel")
case "finish":
    let folder = arguments.count > 2 ? arguments[2] : ""
    let success = arguments.count > 3 ? arguments[3] : "0"
    let total = arguments.count > 4 ? arguments[4] : "0"
    let response = showAlert("整理完成", "已处理 \(success)/\(total) 个视频。\n\n结果文件夹：\(folder)\n\n其中的 CSV 清单记录了原文件名、AI 摘要和处理结果。", ["在 Finder 中打开", "完成"])
    if response == .alertFirstButtonReturn { NSWorkspace.shared.open(URL(fileURLWithPath: folder)) }
case "message":
    let title = arguments.count > 2 ? arguments[2] : "Reelsift"
    let message = arguments.count > 3 ? arguments[3] : ""
    _ = showAlert(title, message, ["好"])
default:
    exit(1)
}
