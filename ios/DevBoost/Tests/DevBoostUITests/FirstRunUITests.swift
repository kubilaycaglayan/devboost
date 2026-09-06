import XCTest
import UIKit

@MainActor
final class FirstRunUITests: XCTestCase {
    private func launchFresh() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["--ui-test-reset-state"]
        app.launch()
        return app
    }

    func testFirstRunShowsAllMainDestinations() {
        let app = launchFresh()
        XCTAssertTrue(app.tabBars.buttons["Home"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.tabBars.buttons["Hosts"].exists)
        XCTAssertTrue(app.tabBars.buttons["Docker"].exists)
        XCTAssertTrue(app.tabBars.buttons["Transfer"].exists)
        XCTAssertTrue(app.tabBars.buttons["Usage"].exists)
    }

    func testHomeTilesDoNotTintTheSurroundingMargin() {
        let app = launchFresh()
        let tileIDs = ["home-feature-hosts", "home-feature-docker", "home-feature-usage"]
        let tiles = tileIDs.map { app.buttons[$0] }

        for tile in tiles {
            XCTAssertTrue(tile.waitForExistence(timeout: 5))
            XCTAssertGreaterThan(tile.frame.minX, 10)
        }

        let screenshot = XCUIScreen.main.screenshot()
        for tile in tiles {
            let samplePoint = CGPoint(x: tile.frame.minX - 8, y: tile.frame.midY)
            let color = screenshot.pixelColor(at: samplePoint)
            XCTAssertLessThan(color.red, 0.10, "Tile decoration leaked into the left margin")
            XCTAssertLessThan(color.green, 0.10, "Tile decoration leaked into the left margin")
            XCTAssertLessThan(color.blue, 0.12, "Tile decoration leaked into the left margin")
        }
    }

    func testFirstRunEmptyStatesExplainWhatToDoNext() {
        let app = launchFresh()
        app.tabBars.buttons["Hosts"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Docker"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Transfer"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))

        app.tabBars.buttons["Usage"].tap()
        XCTAssertTrue(app.staticTexts["No SSH hosts"].waitForExistence(timeout: 5))
    }

    func testFirstTimeHostSetupValidatesConnectionFields() {
        let app = launchFresh()
        app.tabBars.buttons["Hosts"].tap()
        app.buttons["Add SSH host"].tap()

        let save = app.buttons["Save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        XCTAssertFalse(save.isEnabled)
        app.textFields["Hostname or IP"].tap()
        app.textFields["Hostname or IP"].typeText("server.example")
        app.textFields["Username"].tap()
        app.textFields["Username"].typeText("ubuntu")
        XCTAssertTrue(save.isEnabled)
    }
}

private extension XCUIScreenshot {
    func pixelColor(at point: CGPoint) -> (red: CGFloat, green: CGFloat, blue: CGFloat) {
        let image = image.cgImage!
        let scaleX = CGFloat(image.width) / UIScreen.main.bounds.width
        let scaleY = CGFloat(image.height) / UIScreen.main.bounds.height
        let x = Int(point.x * scaleX)
        let y = Int(point.y * scaleY)

        var pixel = [UInt8](repeating: 0, count: 4)
        let context = pixel.withUnsafeMutableBytes {
            CGContext(
                data: $0.baseAddress,
                width: 1,
                height: 1,
                bitsPerComponent: 8,
                bytesPerRow: 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            )!
        }
        context.draw(image, in: CGRect(x: -x, y: -y, width: image.width, height: image.height))
        return (
            CGFloat(pixel[0]) / 255,
            CGFloat(pixel[1]) / 255,
            CGFloat(pixel[2]) / 255
        )
    }
}
